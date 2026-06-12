"""Tests for structured-finding parsing, validation, and the verdict logic."""

from __future__ import annotations

import pytest

from lithos_loom.plugins.story_develop.handoff import (
    HandoffError,
    parse_review_handoff,
    reviewer_handoff_name,
    severity_at_or_above,
)

_LGTM = "## Status: LGTM\n## Summary\nAll good.\n"
_FINDINGS = (
    "## Status: FINDINGS\n"
    "## Summary\nTwo issues found.\n"
    "## Findings\n"
    "- finding_id: f-001\n"
    "  severity: major\n"
    "  status: open\n"
    '  files: ["a.py:10", "b.py:3"]\n'
    "  rationale: missing validation\n"
    "  coder_response:\n"
    "- finding_id: f-002\n"
    "  severity: minor\n"
    "  status: open\n"
    "  files: a.py:20\n"
    "  rationale: nit\n"
)


def test_parse_lgtm() -> None:
    h = parse_review_handoff(_LGTM)
    assert h.is_lgtm
    assert h.status == "LGTM"
    assert h.summary == "All good."
    assert h.findings == []
    assert h.max_open_severity is None
    assert h.passes("major") is True


def test_parse_findings_with_severities_and_files() -> None:
    h = parse_review_handoff(_FINDINGS)
    assert h.status == "FINDINGS"
    assert len(h.findings) == 2
    f1, f2 = h.findings
    assert f1.finding_id == "f-001"
    assert f1.severity == "major"
    assert f1.files == ["a.py:10", "b.py:3"]
    assert f2.files == ["a.py:20"]  # bare comma-less value also parses
    assert h.max_open_severity == "major"


def test_threshold_blocks_and_passes() -> None:
    h = parse_review_handoff(_FINDINGS)
    assert h.passes("major") is False  # a major open finding blocks at major
    assert h.passes("critical") is True  # nothing critical -> passes at critical


def test_resolved_findings_do_not_block() -> None:
    text = _FINDINGS.replace(
        'status: open\n  files: ["a.py:10"', 'status: fixed\n  files: ["a.py:10"'
    )
    h = parse_review_handoff(text)
    # f-001 is now 'fixed' (resolved); only the minor f-002 remains open
    assert h.max_open_severity == "minor"
    assert h.passes("major") is True


def test_empty_handoff_raises() -> None:
    with pytest.raises(HandoffError, match="empty"):
        parse_review_handoff("   ")


def test_missing_status_raises() -> None:
    with pytest.raises(HandoffError, match="Status"):
        parse_review_handoff("## Summary\njust some text\n")


def test_findings_without_entries_raises() -> None:
    with pytest.raises(HandoffError, match="no '## Findings'"):
        parse_review_handoff("## Status: FINDINGS\n## Summary\nclaims findings\n")


def test_invalid_severity_raises() -> None:
    bad = (
        "## Status: FINDINGS\n## Findings\n"
        "- finding_id: f-1\n  severity: huge\n  status: open\n"
    )
    with pytest.raises(HandoffError, match="severity"):
        parse_review_handoff(bad)


def test_invalid_status_value_raises() -> None:
    bad = (
        "## Status: FINDINGS\n## Findings\n"
        "- finding_id: f-1\n  severity: major\n  status: bogus\n"
    )
    with pytest.raises(HandoffError, match="status"):
        parse_review_handoff(bad)


def test_severity_at_or_above() -> None:
    assert severity_at_or_above("critical", "major") is True
    assert severity_at_or_above("minor", "major") is False
    assert severity_at_or_above("major", "major") is True


def test_reviewer_handoff_name() -> None:
    assert reviewer_handoff_name(1, "security") == "round_01_review_security.md"


def test_headers_with_trailing_colon_are_tolerated() -> None:
    # "## Findings:" / "## Summary:" (trailing colon) is a common variant and
    # must not break section lookup (Copilot review on PR #75).
    text = (
        "## Status: FINDINGS\n"
        "## Summary:\nNeeds a guard.\n"
        "## Findings:\n"
        "- finding_id: f-1\n  severity: major\n  status: open\n"
    )
    h = parse_review_handoff(text)
    assert h.status == "FINDINGS"
    assert h.summary == "Needs a guard."
    assert len(h.findings) == 1 and h.findings[0].severity == "major"


def test_folded_scalar_rationale_is_captured() -> None:
    # Reviewers write YAML folded scalars in practice (seen in run c7fa1c8d);
    # the text must be captured, not silently dropped (T7 ledger feeds on it).
    text = (
        "## Status: FINDINGS\n## Summary\nOne issue.\n## Findings\n"
        "- finding_id:\n"
        "  severity: minor\n"
        "  status: open\n"
        "  rationale: >\n"
        "    The alias on line 30 is a redundant duplicate of line 29.\n"
        "    Removing it and using the plain name is cleaner.\n"
        "  coder_response:\n"
    )
    (f,) = parse_review_handoff(text).findings
    assert "redundant duplicate" in f.rationale
    assert "is cleaner" in f.rationale
    assert f.coder_response == ""  # the key AFTER the fold still parses


def test_literal_scalar_and_fold_ends_at_next_item() -> None:
    text = (
        "## Status: FINDINGS\n## Summary\nTwo.\n## Findings\n"
        "- finding_id:\n"
        "  severity: major\n"
        "  status: open\n"
        "  rationale: |\n"
        "    line one\n"
        "    line two\n"
        "- finding_id:\n"
        "  severity: minor\n"
        "  status: open\n"
        "  rationale: plain\n"
    )
    first, second = parse_review_handoff(text).findings
    assert first.rationale == "line one\nline two"
    assert second.rationale == "plain"


def test_blank_finding_id_stays_blank() -> None:
    # Canonical ids are LEDGER-assigned; the parser must not invent fallbacks
    # (a per-file fallback would collide across rounds).
    text = (
        "## Status: FINDINGS\n## Summary\nx.\n## Findings\n"
        "- finding_id:\n  severity: minor\n  status: open\n"
        "- severity: major\n  status: open\n"
    )
    findings = parse_review_handoff(text).findings
    assert [f.finding_id for f in findings] == ["", ""]
