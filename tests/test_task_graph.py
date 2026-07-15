"""Tests for ``lithos_loom.task_graph`` (bulk task import graph builder).

Covers D63 (top-level flat), D64 (indented children become a parent_child
epic), D65 (sibling parallelism + ``[sequential]`` override), D66
(empty-parent validation).

D64/D65 were re-cut for Epic G / US9: the graph is now expressed with
Lithos's real edges (``parent_child`` for containment, ``blocks`` for
order) rather than the old ``metadata.depends_on`` /
``metadata.parallelizable`` encoding, which Lithos now rejects outright.
Two invariants flipped and are pinned explicitly below:

- a parent no longer depends on its children (containment ≠ dependency);
- parallel siblings are the *absence* of edges, not a metadata flag.
"""

from __future__ import annotations

from lithos_loom.task_graph import EPIC, TASK, TaskCreatePlan, build_plan
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
        assert plan.task_type == TASK
        assert plan.parent_line_number is None
        assert plan.depends_on_line_numbers == ()


def test_single_task() -> None:
    plans, errors = _build_from_text("- [ ] Only one\n")
    assert errors == []
    assert len(plans) == 1
    assert plans[0].task_type == TASK
    assert plans[0].parent_line_number is None
    assert plans[0].depends_on_line_numbers == ()


# ── D64: Indented children become an epic + parent_child edges ─────────


def test_single_parent_three_parallel_children() -> None:
    text = "- [ ] Parent\n  - [ ] Child A\n  - [ ] Child B\n  - [ ] Child C\n"
    plans, errors = _build_from_text(text)
    assert errors == []
    # The parent (line 1) is a container, not work.
    assert _by_line(plans, 1).task_type == EPIC
    # Each child points at it — one parent_child edge apiece.
    for child_line in (2, 3, 4):
        assert _by_line(plans, child_line).parent_line_number == 1
        assert _by_line(plans, child_line).task_type == TASK


def test_parent_does_not_depend_on_its_children() -> None:
    """Containment is a parent_child edge, NOT a dependency.

    The old encoding gave the parent ``depends_on = [children]`` because
    Lithos had no edge surface to say "contains". That inverted the real
    relationship, and an epic is excluded from the ready-queue by type
    anyway — so it never needed the dependency to avoid dispatch.
    """
    text = "- [ ] Parent\n  - [ ] Child A\n  - [ ] Child B\n"
    plans, _ = _build_from_text(text)
    assert _by_line(plans, 1).depends_on_line_numbers == ()


def test_parallel_children_have_no_dependencies() -> None:
    """Parallelism is now the absence of ``blocks`` edges, not a flag."""
    text = "- [ ] Parent\n  - [ ] First\n  - [ ] Second\n"
    plans, _ = _build_from_text(text)
    assert _by_line(plans, 2).depends_on_line_numbers == ()
    assert _by_line(plans, 3).depends_on_line_numbers == ()


def test_deeply_nested_children() -> None:
    """Grandchildren are recursively a parent-group under the child."""
    text = "- [ ] Root\n  - [ ] Mid\n    - [ ] Leaf A\n    - [ ] Leaf B\n"
    plans, errors = _build_from_text(text)
    assert errors == []
    # Root and Mid both have children, so both are epics.
    assert _by_line(plans, 1).task_type == EPIC
    assert _by_line(plans, 2).task_type == EPIC
    assert _by_line(plans, 2).parent_line_number == 1
    # Leaves hang off Mid and are real work.
    for leaf in (3, 4):
        assert _by_line(plans, leaf).task_type == TASK
        assert _by_line(plans, leaf).parent_line_number == 2
        assert _by_line(plans, leaf).depends_on_line_numbers == ()


def test_parent_precedes_children_in_plan_order() -> None:
    """Document order is the creation order, and Lithos requires a
    ``parent_task_id`` to already exist — so every child must trail its
    parent in the emitted plans."""
    text = "- [ ] Root\n  - [ ] Mid\n    - [ ] Leaf\n- [ ] Other\n"
    plans, _ = _build_from_text(text)
    position = {p.line.line_number: i for i, p in enumerate(plans)}
    for plan in plans:
        if plan.parent_line_number is not None:
            assert position[plan.parent_line_number] < position[plan.line.line_number]


# ── D65: [sequential] override ─────────────────────────────────────────


def test_sequential_parent_children_form_chain() -> None:
    text = (
        "- [ ] Implement [sequential]\n  - [ ] Step 1\n  - [ ] Step 2\n  - [ ] Step 3\n"
    )
    plans, errors = _build_from_text(text)
    assert errors == []
    # The parent is still just a container — the chain is between siblings.
    assert _by_line(plans, 1).task_type == EPIC
    assert _by_line(plans, 1).depends_on_line_numbers == ()
    # Step 1 (first child) has no sibling-deps
    assert _by_line(plans, 2).depends_on_line_numbers == ()
    # Step 2 depends on Step 1
    assert _by_line(plans, 3).depends_on_line_numbers == (2,)
    # Step 3 depends on Step 2
    assert _by_line(plans, 4).depends_on_line_numbers == (3,)


def test_sequential_chain_predecessors_precede_dependents_in_plan_order() -> None:
    """``depends_on`` predecessors must already exist at create time."""
    text = "- [ ] Build [sequential]\n  - [ ] A\n  - [ ] B\n  - [ ] C\n"
    plans, _ = _build_from_text(text)
    position = {p.line.line_number: i for i, p in enumerate(plans)}
    for plan in plans:
        for dep in plan.depends_on_line_numbers:
            assert position[dep] < position[plan.line.line_number]


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
    # Feature B's children: sequential chain
    assert _by_line(plans, 5).depends_on_line_numbers == ()
    assert _by_line(plans, 6).depends_on_line_numbers == (5,)
    # Both parents are epics; the marker only changes sibling edges.
    assert _by_line(plans, 1).task_type == EPIC
    assert _by_line(plans, 4).task_type == EPIC


def test_sequential_marker_on_a_leaf_has_no_effect() -> None:
    """``[sequential]`` orders a parent's children; on a childless line
    there are no children to order, and it must not leak to its own
    siblings (which belong to a different — here absent — parent)."""
    text = "- [ ] A [sequential]\n- [ ] B [sequential]\n"
    plans, errors = _build_from_text(text)
    assert errors == []
    for plan in plans:
        assert plan.task_type == TASK
        assert plan.depends_on_line_numbers == ()


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
    assert _by_line(plans, 1).task_type == EPIC
    assert _by_line(plans, 2).parent_line_number == 1


def test_four_space_indented_children() -> None:
    text = "- [ ] Parent\n    - [ ] Child\n"
    plans, _ = _build_from_text(text)
    assert _by_line(plans, 1).task_type == EPIC
    assert _by_line(plans, 2).parent_line_number == 1


def test_returning_to_top_level_after_nest() -> None:
    """After a nested group, a fresh top-level task is independent."""
    text = (
        "- [ ] Group 1\n"  # 1
        "  - [ ] G1.A\n"  # 2
        "- [ ] Group 2\n"  # 3 — back at top level
    )
    plans, _ = _build_from_text(text)
    assert _by_line(plans, 1).task_type == EPIC
    assert _by_line(plans, 2).parent_line_number == 1
    # Group 2 is truly independent: no parent, no deps, and not an epic.
    assert _by_line(plans, 3).task_type == TASK
    assert _by_line(plans, 3).parent_line_number is None
    assert _by_line(plans, 3).depends_on_line_numbers == ()
