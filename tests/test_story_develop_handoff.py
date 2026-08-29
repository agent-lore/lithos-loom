"""Tests for structured-finding parsing, validation, and the verdict logic."""

from __future__ import annotations

from pathlib import Path

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


def test_folded_scalar_keeps_embedded_bullet_lists() -> None:
    # Bullet lists are common inside YAML text blocks; a more-indented "- "
    # line is fold CONTENT, not a new finding item (Copilot review on PR #80).
    text = (
        "## Status: FINDINGS\n## Summary\nOne.\n## Findings\n"
        "- finding_id:\n"
        "  severity: major\n"
        "  status: open\n"
        "  rationale: >\n"
        "    Two problems:\n"
        "    - the lock is taken twice\n"
        "    - the error path leaks the fd\n"
        "- finding_id:\n"
        "  severity: minor\n"
        "  status: open\n"
        "  rationale: separate item\n"
    )
    first, second = parse_review_handoff(text).findings
    assert "- the lock is taken twice" in first.rationale
    assert "- the error path leaks the fd" in first.rationale
    assert second.severity == "minor" and second.rationale == "separate item"


def test_conversation_log_includes_artifact_pass_handoffs(tmp_path: Path) -> None:
    # #291: the review that actually controlled approval (the artifact pass)
    # must appear in the durable audit trail; absent files render nothing.
    from lithos_loom.plugins.story_develop import handoff as h

    d = tmp_path
    (d / h.coder_handoff_name(1)).write_text("did the work")
    (d / h.reviewer_handoff_name(1, "correctness")).write_text("LGTM early")
    (d / h.reviewer_handoff_name(1, "correctness_artifacts")).write_text(
        "visual findings"
    )

    log = h.conversation_log(d, 1, ["correctness"])

    assert "artifact pass" in log
    assert "visual findings" in log
    assert log.index("LGTM early") < log.index("visual findings")


def test_conversation_log_omits_absent_artifact_handoffs(tmp_path: Path) -> None:
    from lithos_loom.plugins.story_develop import handoff as h

    d = tmp_path
    (d / h.coder_handoff_name(1)).write_text("did the work")
    (d / h.reviewer_handoff_name(1, "correctness")).write_text("LGTM")

    log = h.conversation_log(d, 1, ["correctness"])

    assert "artifact pass" not in log


# ── out-of-scope disposition (819370e5) ────────────────────────────────


def test_out_of_scope_does_not_block() -> None:
    # The escape's whole point: a REAL finding that is not this story's to fix
    # is resolved — it never counts toward the reviewer's block threshold.
    text = (
        "## Status: FINDINGS\n## Summary\ns\n## Findings\n"
        "- finding_id: f-1\n  severity: critical\n  status: out-of-scope\n"
        "  rationale: pre-existing on the base; filed as its own task\n"
    )
    h = parse_review_handoff(text)
    assert h.max_open_severity is None
    assert h.passes("minor") is True


def test_out_of_scope_without_rationale_is_rejected() -> None:
    # The disposition is a licence to not-block; the stated WHY is its
    # counterweight (819370e5's guardrail). No rationale -> malformed handoff
    # -> the reviewer is re-prompted, same as an invalid status.
    bad = (
        "## Status: FINDINGS\n## Summary\ns\n## Findings\n"
        "- finding_id: f-1\n  severity: major\n  status: out-of-scope\n"
    )
    with pytest.raises(HandoffError, match="out-of-scope.*WHY"):
        parse_review_handoff(bad)
