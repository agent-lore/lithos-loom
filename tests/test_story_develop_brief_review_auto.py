"""Brief review at dispatch, phase 2 (10cc6310): the structural check.

An addendum may skip its gate only when the pass labelled every item a fact
AND every fact passes three structural rules — the heading prescribes
nothing, the body cites a repository path, and no decision line hides in it.
Every failure direction holds the story.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop.brief_review import (
    KIND_DECISION,
    KIND_FACT,
    KIND_SCOPE_CUT,
    MODE_DELTA,
    Addendum,
    BriefItem,
    parse_addendum,
)
from lithos_loom.plugins.story_develop.brief_review_auto import (
    HEADING_MARKERS,
    AutoFactsVerdict,
    check_auto_facts,
    describe_verdict,
    fact_headings,
    fact_problems,
    item_heading,
    pr_section,
)

FIXTURES = Path(__file__).parent / "fixtures" / "brief_review"


def _fact(text: str, item_id: str = "F1") -> BriefItem:
    return BriefItem(id=item_id, kind=KIND_FACT, text=text)


def _good(item_id: str = "F1") -> BriefItem:
    return _fact(f"**{item_id}. W7 added no refusal hook.** `src/f.py:536`.", item_id)


def test_the_heading_is_the_bold_title_after_the_id() -> None:
    assert item_heading(_good()) == "W7 added no refusal hook."
    assert item_heading(_fact("**F1.** `a.py`")) is None
    assert item_heading(_fact("F1. No bold. `a.py`")) is None


def test_a_good_fact_has_no_problems() -> None:
    assert fact_problems(_good()) == []


@pytest.mark.parametrize("marker", HEADING_MARKERS)
def test_every_heading_marker_reads_as_a_decision(marker: str) -> None:
    item = _fact(f"**F3. The router {marker.upper()} the thing.** `src/r.py:1`.", "F3")

    problems = fact_problems(item)

    assert problems == [f"F3 reads like a decision: heading says `{marker}`"]


def test_the_markers_are_the_sixteen_the_rule_names() -> None:
    assert len(HEADING_MARKERS) == 16


@pytest.mark.parametrize(
    "heading",
    [
        "W7 added no refusal hook.",
        "The funnel uses a WriteProblem.",
        "The module makes no network call.",
        "Movement of the cursor is lazy.",
        "Admission counts only held stories.",
    ],
)
def test_a_marker_must_be_a_whole_word(heading: str) -> None:
    assert fact_problems(_fact(f"**F1. {heading}** `src/a.py:3`")) == []


def test_a_marker_in_the_body_is_not_the_heading_rule() -> None:
    # The corpus: body markers flag ~20% of genuine facts; headings one.
    item = _fact("**F1. The route exists.** It must stay. `src/a.py:3`")
    assert fact_problems(item) == []


def test_a_multi_word_marker_spans_any_whitespace() -> None:
    item = _fact("**F1. The routes go  in r.py.** `src/r.py:1`")
    assert fact_problems(item) == ["F1 reads like a decision: heading says `go in`"]


def test_every_marker_in_a_heading_is_named_on_one_line() -> None:
    item = _fact("**F1. Must use the new module.** `src/r.py:1`")
    assert fact_problems(item) == [
        "F1 reads like a decision: heading says `must`, `use`"
    ]


@pytest.mark.parametrize(
    "span",
    [
        "a.py:10-20",
        "write_funnel.py:536",
        "src/lithos_loom/gates.py:283",
        "docs/x.md",
        ".github/workflows/ci.yml",
        "pyproject.toml",
    ],
)
def test_a_backticked_repository_path_is_a_citation(span: str) -> None:
    assert fact_problems(_fact(f"**F1. The code is there.** `{span}`")) == []


@pytest.mark.parametrize(
    "body",
    [
        "`brief_review.Addendum`",  # a dotted identifier alone
        "`x.py:12 perform()`",  # other text in the span
        "`Makefile`",  # no extension
        "`a.exe`",  # an extension outside the set
        "src/a.py:12",  # not backticked
        "no citation at all",
    ],
)
def test_anything_else_fails_the_citation_rule(body: str) -> None:
    problems = fact_problems(_fact(f"**F2. The code is there.** {body}", "F2"))

    assert len(problems) == 1
    assert problems[0].startswith("F2 reads like a decision: it cites no repository")


@pytest.mark.parametrize("line", ["  - Basis: F1.", "  - Proposed: a new module."])
def test_a_decision_line_inside_a_fact_reads_as_a_decision(line: str) -> None:
    item = _fact(f"**F1. The code is there.** `src/a.py:1`\n{line}")

    problems = fact_problems(item)

    assert len(problems) == 1
    assert line.split()[1] in problems[0]  # `Basis:` / `Proposed:`


def test_backticks_do_not_exempt_a_marker() -> None:
    item = _fact("**F1. The `make` target exists.** `src/a.py:1`")
    assert fact_problems(item) == ["F1 reads like a decision: heading says `make`"]


def test_a_malformed_heading_is_a_problem() -> None:
    problems = fact_problems(_fact("**F1.** `src/a.py:1`"))

    assert len(problems) == 1 and "heading could not be read" in problems[0]


def test_facts_only_needs_items_all_labelled_facts() -> None:
    assert check_auto_facts(Addendum(items=(_good(),))).passed
    assert not check_auto_facts(Addendum()).facts_only
    assert not check_auto_facts(Addendum(no_change="Nothing moved.")).facts_only
    for kind in (KIND_DECISION, KIND_SCOPE_CUT, None):
        other = BriefItem(id="D1", kind=kind, text="**D1. X.**\n  - Basis: F1.")
        verdict = check_auto_facts(Addendum(items=(_good(), other)))
        assert not verdict.facts_only and not verdict.passed


def test_facts_only_refuses_stray_lines() -> None:
    # A validated draft never has any (review_brief degrades on them); the
    # guard stays for a parse that someday lets one through.
    verdict = check_auto_facts(Addendum(items=(_good(),), stray=("- **D1: x**",)))
    assert not verdict.facts_only


def test_facts_only_refuses_items_alongside_no_change() -> None:
    verdict = check_auto_facts(Addendum(items=(_good(),), no_change="Nothing."))
    assert not verdict.facts_only


def test_a_facts_only_draft_with_a_prescriptive_fact_does_not_pass() -> None:
    draft = parse_addendum(
        "## Facts\n\n"
        "- **F1. Put the cancel routes in a new module.** `routes.py:1`\n"
        "- **F2. The router exists.** `src/r.py:4`\n"
    )

    verdict = check_auto_facts(draft)

    assert verdict.facts_only and not verdict.passed
    assert verdict.problems == ("F1 reads like a decision: heading says `put`",)


@pytest.mark.parametrize("name", ["w6", "w7", "w8"])
def test_the_pilot_addenda_are_never_auto_eligible(name: str) -> None:
    text = (FIXTURES / f"lens-t3-{name}.md").read_text(encoding="utf-8")

    verdict = check_auto_facts(parse_addendum(text))

    assert not verdict.facts_only and not verdict.passed


def test_the_record_shape() -> None:
    assert AutoFactsVerdict(facts_only=True).as_record() == {
        "facts_only": True,
        "passed": True,
        "problems": [],
    }
    assert AutoFactsVerdict(facts_only=True, problems=("F1 x",)).as_record() == {
        "facts_only": True,
        "passed": False,
        "problems": ["F1 x"],
    }


def test_fact_headings_are_id_and_heading() -> None:
    addendum = Addendum(items=(_good("F13"), _fact("**F2.** `a.py`", "F2")))

    assert fact_headings(addendum) == (
        "F13. W7 added no refusal hook.",
        "F2. **F2.** `a.py`",
    )


def test_the_pr_section_fences_the_headings() -> None:
    section = pr_section(("F13. W7 added no refusal hook. Closes #1",))

    lines = section.splitlines()
    assert lines[0] == "## Brief review"
    assert "without a gate" in section and "*What*" in section
    opened = next(i for i, line in enumerate(lines) if line.startswith("```"))
    heading = next(i for i, line in enumerate(lines) if "F13." in line)
    # The agent's headings sit inside the fence, which closes the section.
    assert opened < heading < len(lines) - 1
    assert lines[-1].startswith("```") and lines[-1].strip("`") == ""


def test_the_pr_section_is_empty_for_no_facts() -> None:
    assert pr_section(()) == ""


def test_describe_verdict_says_what_dispatch_would_do() -> None:
    counts = {"scope_cut": 0, "fact": 1, "decision": 0}
    passed = AutoFactsVerdict(facts_only=True)
    failed = AutoFactsVerdict(
        facts_only=True, problems=("F3 reads like a decision: x",)
    )
    mixed = AutoFactsVerdict(facts_only=False)

    assert describe_verdict(passed, counts=counts) == (
        "auto-facts check: would skip the gate with develop_brief_review_auto_facts on"
    )
    assert describe_verdict(passed, counts=counts, mode=MODE_DELTA) == (
        "auto-facts check: would append without a gate"
    )
    assert describe_verdict(failed, counts=counts) == (
        "auto-facts check: would hold: F3 reads like a decision: x"
    )
    assert describe_verdict(mixed, counts={**counts, "decision": 1}) == (
        "auto-facts check: not facts only (1 decision): would hold"
    )
    none = {"scope_cut": 0, "fact": 0, "decision": 0}
    assert describe_verdict(mixed, counts=none).startswith(
        "auto-facts check: no change — would skip the gate"
    )
