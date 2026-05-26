"""Tests for ``lithos_loom.task_line_parser`` (bulk task import parser).

Pure tests — the parser is I/O-free. Covers the PRD line filter
(D67), tag regex (D40/D61), priority emoji mapping (D61),
``#project/<slug>`` extraction (D62), ``[sequential]`` marker
detection (D65), and empty-parent detection (D66).
"""

from __future__ import annotations

import pytest

from lithos_loom.task_line_parser import (
    PRIORITY_EMOJI_MAP,
    TAG_REGEX,
    ParsedTaskLine,
    ValidationError,
    parse_doc,
)

_SLUG = "demo-project"


def _parse(
    text: str, slug: str = _SLUG
) -> tuple[list[ParsedTaskLine], list[ValidationError], str]:
    return parse_doc(text, slug)


# ── Line filter (D67) ──────────────────────────────────────────────────


def test_flat_top_level_three_tasks() -> None:
    text = "- [ ] First\n- [ ] Second\n- [ ] Third\n"
    parsed, errors, body = _parse(text)
    assert [p.description for p in parsed] == ["First", "Second", "Third"]
    assert errors == []
    assert body == ""


def test_open_marker_only_other_markers_ignored() -> None:
    text = (
        "- [ ] Open task\n"
        "- [x] Done task\n"
        "- [/] In-progress\n"
        "- [-] Cancelled\n"
        "- [>] Forwarded\n"
    )
    parsed, _, body = _parse(text)
    assert [p.description for p in parsed] == ["Open task"]
    # All non-`[ ]` markers stay verbatim in the body
    assert "[x]" in body
    assert "[/]" in body
    assert "[-]" in body
    assert "[>]" in body


def test_extracted_lines_stripped_from_body() -> None:
    text = "Intro paragraph.\n\n- [ ] Task one\n\nMore prose.\n"
    _, _, body = _parse(text)
    assert "Task one" not in body
    assert "Intro paragraph." in body
    assert "More prose." in body


def test_star_and_plus_list_markers_not_parsed() -> None:
    text = "* [ ] Star list\n+ [ ] Plus list\n- [ ] Dash list\n"
    parsed, _, _ = _parse(text)
    assert [p.description for p in parsed] == ["Dash list"]


def test_fenced_code_block_backticks_ignored() -> None:
    text = "```\n- [ ] Example task in code\n```\n- [ ] Real task\n"
    parsed, _, body = _parse(text)
    assert [p.description for p in parsed] == ["Real task"]
    assert "Example task in code" in body  # stays verbatim in code block


def test_fenced_code_block_tildes_ignored() -> None:
    text = "~~~\n- [ ] Tilde-fenced example\n~~~\n- [ ] Real one\n"
    parsed, _, _ = _parse(text)
    assert [p.description for p in parsed] == ["Real one"]


def test_fenced_code_block_longer_fence() -> None:
    """4-backtick fence requires 4+ backticks to close (CommonMark)."""
    text = (
        "````\n"
        "```\n"  # 3-backtick line inside should NOT close the 4-backtick block
        "- [ ] Inside\n"
        "```\n"
        "````\n"
        "- [ ] Outside\n"
    )
    parsed, _, _ = _parse(text)
    assert [p.description for p in parsed] == ["Outside"]


def test_blockquote_skipped() -> None:
    text = "> - [ ] Quoted task\n- [ ] Real task\n"
    parsed, _, body = _parse(text)
    assert [p.description for p in parsed] == ["Real task"]
    assert "Quoted task" in body


def test_indented_blockquote_skipped() -> None:
    text = "  > - [ ] Indented quote\n- [ ] Real task\n"
    parsed, _, _ = _parse(text)
    assert [p.description for p in parsed] == ["Real task"]


# ── Tag regex (D40 / D61) ──────────────────────────────────────────────


def test_tag_regex_matches_slash_and_hyphen() -> None:
    assert TAG_REGEX.findall(" #foo-bar #foo/bar #foo_bar") == [
        "#foo-bar",
        "#foo/bar",
        "#foo_bar",
    ]


def test_tag_regex_requires_whitespace_boundary() -> None:
    # `foo#bar` should NOT be parsed as tag `bar` per Obsidian convention
    assert TAG_REGEX.findall("foo#bar") == []
    # But `#bar` at start-of-string is fine
    assert TAG_REGEX.findall("#bar") == ["#bar"]


def test_tag_parsing_per_line() -> None:
    text = "- [ ] Task with #foo and #bar tags\n"
    parsed, _, _ = _parse(text)
    assert parsed[0].tags == ("foo", "bar")
    assert "#foo" not in parsed[0].description
    assert "#bar" not in parsed[0].description


def test_tags_deduped() -> None:
    text = "- [ ] Task #foo #foo #bar\n"
    parsed, _, _ = _parse(text)
    assert parsed[0].tags == ("foo", "bar")


def test_all_digit_tag_not_parsed_kept_in_text() -> None:
    text = "- [ ] See issue #123 for context\n"
    parsed, _, _ = _parse(text)
    assert parsed[0].tags == ()
    assert "#123" in parsed[0].description


def test_unicode_in_description_preserved() -> None:
    text = "- [ ] Réviser le café résumé\n"
    parsed, _, _ = _parse(text)
    assert parsed[0].description == "Réviser le café résumé"


# ── Priority emojis (D61) ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "emoji,expected",
    [
        ("🔺", "highest"),
        ("⏫", "high"),
        ("🔼", "medium"),
        ("🔽", "low"),
        ("⏬", "lowest"),
    ],
)
def test_priority_emoji_each(emoji: str, expected: str) -> None:
    text = f"- [ ] Task with {emoji} priority\n"
    parsed, _, _ = _parse(text)
    assert parsed[0].priority == expected
    assert emoji not in parsed[0].description


def test_priority_emoji_map_covers_all_five() -> None:
    """Pin the enum: loss of an entry breaks both this and the parametrized test."""
    assert set(PRIORITY_EMOJI_MAP.values()) == {
        "highest",
        "high",
        "medium",
        "low",
        "lowest",
    }


def test_no_priority_emoji_returns_none() -> None:
    parsed, _, _ = _parse("- [ ] Plain task\n")
    assert parsed[0].priority is None


def test_multiple_priority_emojis_highest_precedence_wins() -> None:
    """Dict order = precedence: 🔺 highest wins over ⏫ high."""
    text = "- [ ] Task 🔺 ⏫ both\n"
    parsed, _, _ = _parse(text)
    assert parsed[0].priority == "highest"
    # Both emojis stripped from description
    assert "🔺" not in parsed[0].description
    assert "⏫" not in parsed[0].description


# ── #project/<slug> extraction (D62) ────────────────────────────────────


def test_self_project_tag_silently_consumed() -> None:
    text = f"- [ ] Task #project/{_SLUG} here\n"
    parsed, errors, _ = _parse(text)
    assert parsed[0].cross_project_tag is None
    assert errors == []
    assert "#project" not in parsed[0].description


def test_cross_project_tag_flagged() -> None:
    text = "- [ ] Task #project/other-slug here\n"
    parsed, errors, _ = _parse(text)
    assert parsed[0].cross_project_tag == "project/other-slug"
    assert len(errors) == 1
    assert errors[0].kind == "cross_project_tag"
    assert errors[0].line_number == 1
    assert "other-slug" in errors[0].message


def test_cross_project_tag_does_not_appear_in_regular_tags() -> None:
    text = "- [ ] Task #foo #project/other #bar\n"
    parsed, _, _ = _parse(text)
    assert parsed[0].tags == ("foo", "bar")  # project tag is NOT in tags
    assert parsed[0].cross_project_tag == "project/other"


def test_multiple_cross_project_tags_first_wins() -> None:
    """Only first cross-project tag is captured (whole import aborts anyway)."""
    text = "- [ ] Task #project/a #project/b\n"
    parsed, errors, _ = _parse(text)
    assert parsed[0].cross_project_tag == "project/a"
    # Single error emitted per task line; second project tag stripped silently
    assert len(errors) == 1


# ── [sequential] marker (D65) ──────────────────────────────────────────


def test_sequential_marker_detected_and_stripped() -> None:
    text = "- [ ] Implement [sequential]\n"
    parsed, _, _ = _parse(text)
    assert parsed[0].is_sequential_parent is True
    assert "[sequential]" not in parsed[0].description
    assert parsed[0].description == "Implement"


def test_no_sequential_marker() -> None:
    text = "- [ ] Implement\n"
    parsed, _, _ = _parse(text)
    assert parsed[0].is_sequential_parent is False


def test_sequential_marker_case_sensitive() -> None:
    """``[Sequential]`` (capitalised) should NOT trigger the marker."""
    text = "- [ ] Implement [Sequential]\n"
    parsed, _, _ = _parse(text)
    assert parsed[0].is_sequential_parent is False
    # Capitalised variant stays in the description
    assert "[Sequential]" in parsed[0].description


# ── Empty-parent detection (D66 prep) ──────────────────────────────────


def test_empty_task_flagged() -> None:
    """`- [ ]` with nothing after marks as empty (graph builder uses this for D66)."""
    text = "- [ ]\n  - [ ] child\n"
    parsed, _, _ = _parse(text)
    assert parsed[0].is_empty is True
    assert parsed[1].is_empty is False


def test_task_with_only_tags_is_empty() -> None:
    """A task that's just a tag (no description) reads as empty after stripping."""
    text = "- [ ] #foo\n"
    parsed, _, _ = _parse(text)
    assert parsed[0].is_empty is True
    assert parsed[0].tags == ("foo",)


def test_task_with_only_priority_is_empty() -> None:
    text = "- [ ] ⏫\n"
    parsed, _, _ = _parse(text)
    assert parsed[0].is_empty is True
    assert parsed[0].priority == "high"


# ── Indent tracking ────────────────────────────────────────────────────


def test_indent_count_spaces() -> None:
    text = "- [ ] Parent\n  - [ ] Child2\n    - [ ] Grandchild4\n"
    parsed, _, _ = _parse(text)
    assert [p.indent for p in parsed] == [0, 2, 4]


def test_indent_count_tabs() -> None:
    text = "- [ ] Parent\n\t- [ ] Child1\n\t\t- [ ] Grandchild2\n"
    parsed, _, _ = _parse(text)
    assert [p.indent for p in parsed] == [0, 1, 2]


def test_line_numbers_one_indexed_and_skip_non_task_lines() -> None:
    text = (
        "Intro\n"  # line 1
        "\n"  # line 2
        "- [ ] First\n"  # line 3
        "Prose\n"  # line 4
        "- [ ] Second\n"  # line 5
    )
    parsed, _, _ = _parse(text)
    assert [p.line_number for p in parsed] == [3, 5]


# ── Body round-trip ────────────────────────────────────────────────────


def test_trailing_newline_preserved() -> None:
    """`text` ending with `\\n` round-trips through the stripped body."""
    text = "Heading\n\n- [ ] Task\n\nFooter\n"
    _, _, body = _parse(text)
    assert body.endswith("\n")
    assert "Footer" in body


def test_no_trailing_newline_no_added() -> None:
    text = "Heading\n- [ ] Task"  # no trailing newline
    _, _, body = _parse(text)
    assert not body.endswith("\n")


def test_doc_with_only_tasks_yields_empty_body() -> None:
    text = "- [ ] One\n- [ ] Two\n"
    parsed, _, body = _parse(text)
    assert len(parsed) == 2
    assert body == ""


# ── Validation aggregation ─────────────────────────────────────────────


def test_multiple_cross_project_tags_across_lines_all_reported() -> None:
    """Validate-all-then-abort: each offending line gets its own error."""
    text = (
        "- [ ] OK task\n- [ ] Bad #project/other-a\n- [ ] Also bad #project/other-b\n"
    )
    _, errors, _ = _parse(text)
    assert len(errors) == 2
    assert {e.line_number for e in errors} == {2, 3}
