"""Tests for ``lithos_loom.task_graph`` (bulk task import graph builder).

Covers D63 (top-level flat), D64 (parent.depends_on = children), D65
(sibling parallelism + ``[sequential]`` override), D66 (empty-parent
validation).
"""

from __future__ import annotations

from lithos_loom.task_graph import TaskCreatePlan, build_plan
from lithos_loom.task_line_parser import ValidationError, parse_doc


def _build_from_text(
    text: str, slug: str = "demo"
) -> tuple[list[TaskCreatePlan], list[ValidationError]]:
    lines, _, _ = parse_doc(text, slug)
    plans, errors = build_plan(lines)
    return plans, list(errors)


def _by_line(plans: list[TaskCreatePlan], line_number: int) -> TaskCreatePlan:
    for plan in plans:
        if plan.line.line_number == line_number:
            return plan
    raise AssertionError(f"no plan for line {line_number}")


# ── D63: Top-level flat ────────────────────────────────────────────────


def test_flat_top_level_no_edges() -> None:
    text = "- [ ] A\n- [ ] B\n- [ ] C\n"
    plans, errors = _build_from_text(text)
    assert errors == []
    assert len(plans) == 3
    for plan in plans:
        assert plan.depends_on_line_numbers == ()
        assert plan.parallelizable is False  # top-level has no parallelism contract


def test_single_task() -> None:
    plans, errors = _build_from_text("- [ ] Only one\n")
    assert errors == []
    assert len(plans) == 1
    assert plans[0].depends_on_line_numbers == ()


# ── D64: Parent depends on children (composition) ──────────────────────


def test_single_parent_three_parallel_children() -> None:
    text = "- [ ] Parent\n  - [ ] Child A\n  - [ ] Child B\n  - [ ] Child C\n"
    plans, errors = _build_from_text(text)
    assert errors == []
    # Parent (line 1) depends on all three children (lines 2, 3, 4)
    parent_plan = _by_line(plans, 1)
    assert set(parent_plan.depends_on_line_numbers) == {2, 3, 4}
    # Children depend on nothing (D64: no back-edge to parent)
    for child_line in (2, 3, 4):
        assert _by_line(plans, child_line).depends_on_line_numbers == ()


def test_children_have_parallelizable_true_by_default() -> None:
    text = "- [ ] Parent\n  - [ ] First\n  - [ ] Second\n"
    plans, _ = _build_from_text(text)
    assert _by_line(plans, 2).parallelizable is True
    assert _by_line(plans, 3).parallelizable is True


def test_parent_is_not_parallelizable() -> None:
    """Parents gate on children; they aren't part of a sibling parallel group."""
    text = "- [ ] Parent\n  - [ ] Child\n"
    plans, _ = _build_from_text(text)
    assert _by_line(plans, 1).parallelizable is False


def test_deeply_nested_children() -> None:
    """Grandchildren are recursively a parent-group under the child."""
    text = "- [ ] Root\n  - [ ] Mid\n    - [ ] Leaf A\n    - [ ] Leaf B\n"
    plans, errors = _build_from_text(text)
    assert errors == []
    # Root depends on Mid (line 2)
    assert _by_line(plans, 1).depends_on_line_numbers == (2,)
    # Mid depends on its two leaves (lines 3, 4)
    assert set(_by_line(plans, 2).depends_on_line_numbers) == {3, 4}
    # Leaves depend on nothing
    assert _by_line(plans, 3).depends_on_line_numbers == ()
    assert _by_line(plans, 4).depends_on_line_numbers == ()
    # Leaves are parallelizable (siblings under non-sequential parent Mid)
    assert _by_line(plans, 3).parallelizable is True
    assert _by_line(plans, 4).parallelizable is True


# ── D65: [sequential] override ─────────────────────────────────────────


def test_sequential_parent_children_form_chain() -> None:
    text = (
        "- [ ] Implement [sequential]\n  - [ ] Step 1\n  - [ ] Step 2\n  - [ ] Step 3\n"
    )
    plans, errors = _build_from_text(text)
    assert errors == []
    # Parent depends on all three children (D64 unchanged)
    assert set(_by_line(plans, 1).depends_on_line_numbers) == {2, 3, 4}
    # Step 1 (first child) has no sibling-deps
    assert _by_line(plans, 2).depends_on_line_numbers == ()
    # Step 2 depends on Step 1
    assert _by_line(plans, 3).depends_on_line_numbers == (2,)
    # Step 3 depends on Step 2
    assert _by_line(plans, 4).depends_on_line_numbers == (3,)


def test_sequential_children_not_parallelizable() -> None:
    text = "- [ ] Build [sequential]\n  - [ ] A\n  - [ ] B\n"
    plans, _ = _build_from_text(text)
    assert _by_line(plans, 2).parallelizable is False
    assert _by_line(plans, 3).parallelizable is False


def test_mixed_sequential_and_parallel_groups() -> None:
    text = (
        "- [ ] Feature A\n"  # 1, parallel children
        "  - [ ] A1\n"  # 2
        "  - [ ] A2\n"  # 3
        "- [ ] Feature B [sequential]\n"  # 4, sequential children
        "  - [ ] B1\n"  # 5
        "  - [ ] B2\n"  # 6
    )
    plans, errors = _build_from_text(text)
    assert errors == []
    # Feature A's children: parallel, no inter-deps
    assert _by_line(plans, 2).depends_on_line_numbers == ()
    assert _by_line(plans, 3).depends_on_line_numbers == ()
    assert _by_line(plans, 2).parallelizable is True
    assert _by_line(plans, 3).parallelizable is True
    # Feature B's children: sequential chain
    assert _by_line(plans, 5).depends_on_line_numbers == ()
    assert _by_line(plans, 6).depends_on_line_numbers == (5,)
    assert _by_line(plans, 5).parallelizable is False
    assert _by_line(plans, 6).parallelizable is False


# ── D66: Empty-parent error ────────────────────────────────────────────


def test_empty_parent_with_children_errors() -> None:
    text = "- [ ]\n  - [ ] Real child\n"
    _, errors = _build_from_text(text)
    assert len(errors) == 1
    assert errors[0].line_number == 1
    assert errors[0].kind == "empty_parent"


def test_empty_leaf_is_not_an_error() -> None:
    """Empty leaf is fine; it becomes a vacuous Lithos task — not importer's concern."""
    text = "- [ ]\n"
    _, errors = _build_from_text(text)
    assert errors == []


def test_empty_parent_with_no_children_is_not_an_error() -> None:
    """The error fires ONLY when the empty task has indented children below."""
    text = "- [ ]\n- [ ] Sibling at same level\n"
    _, errors = _build_from_text(text)
    assert errors == []


def test_multiple_empty_parents_all_reported() -> None:
    """Validate-all-then-abort: report every empty parent in one pass."""
    text = "- [ ]\n  - [ ] Child A\n- [ ]\n  - [ ] Child B\n"
    _, errors = _build_from_text(text)
    assert len(errors) == 2
    assert {e.line_number for e in errors} == {1, 3}


# ── Indent-style edge cases ────────────────────────────────────────────


def test_tab_indented_children() -> None:
    """Pure-tab indentation infers parent-child the same way as spaces."""
    text = "- [ ] Parent\n\t- [ ] Child\n"
    plans, errors = _build_from_text(text)
    assert errors == []
    assert _by_line(plans, 1).depends_on_line_numbers == (2,)
    assert _by_line(plans, 2).parallelizable is True


def test_four_space_indented_children() -> None:
    text = "- [ ] Parent\n    - [ ] Child\n"
    plans, _ = _build_from_text(text)
    assert _by_line(plans, 1).depends_on_line_numbers == (2,)


def test_returning_to_top_level_after_nest() -> None:
    """After a nested group, a fresh top-level task is independent."""
    text = (
        "- [ ] Group 1\n"  # 1
        "  - [ ] G1.A\n"  # 2
        "- [ ] Group 2\n"  # 3 — back at top level
    )
    plans, _ = _build_from_text(text)
    assert _by_line(plans, 1).depends_on_line_numbers == (2,)
    assert _by_line(plans, 3).depends_on_line_numbers == ()  # truly independent
    assert _by_line(plans, 3).parallelizable is False  # top-level
