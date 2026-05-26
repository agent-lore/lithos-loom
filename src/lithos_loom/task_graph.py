"""Pure dependency-graph builder for bulk task import.

Takes the list of ``ParsedTaskLine`` produced by ``task_line_parser``
and returns ``TaskCreatePlan`` entries with dependency edges derived
from indentation per the locked decisions in
``docs/prd/bulk-task-import.md``:

- **D63** Top-level tasks are flat (no ``depends_on`` between them).
- **D64** Indented children represent composition: parent gets
  ``metadata.depends_on = [child_line_numbers]``; children have NO
  ``depends_on`` back to the parent. Parent is marked complete
  manually after all children are done.
- **D65** Sibling children of a parent are parallelizable by default
  (``parallelizable = True``, no ``depends_on`` between siblings).
  When the parent carries the ``[sequential]`` marker
  (``is_sequential_parent = True``), that parent's children form a
  chain: child[i] depends on child[i-1].
- **D66** Parent tasks with empty descriptions (just a heading) are
  flagged as validation errors.

The builder is I/O-free. Line-number references in
``depends_on_line_numbers`` are resolved to Lithos task ids at
execution time by the CLI layer.
"""

from __future__ import annotations

from dataclasses import dataclass

from lithos_loom.task_line_parser import ParsedTaskLine, ValidationError


@dataclass(frozen=True)
class TaskCreatePlan:
    """One Lithos ``task_create`` call's worth of structured input.

    ``depends_on_line_numbers`` references other ``ParsedTaskLine``s
    by their ``line_number`` field. The CLI layer creates tasks in
    topological order (children before parents) and resolves each
    line number to a freshly-minted Lithos task id when the parent's
    turn comes.
    """

    line: ParsedTaskLine
    depends_on_line_numbers: tuple[int, ...]
    parallelizable: bool


def build_plan(
    lines: list[ParsedTaskLine],
) -> tuple[list[TaskCreatePlan], list[ValidationError]]:
    """Build task-create plans with dependency edges from indentation.

    Walks ``lines`` in document order, tracking a stack of open
    ancestors. A line whose indent is deeper than the top of the
    stack is a child of the top. A line whose indent is the same as
    or shallower than the top pops the stack until it would be a
    valid child (or the stack is empty, meaning it's a top-level
    task).

    Returns:
        (plans, errors). ``plans`` preserves the input order (caller
        topologically sorts at task-create time). ``errors`` contains
        D66 empty-parent violations only — D62 cross-project tag
        violations come from the parser itself and are passed
        through the CLI separately.
    """
    plans: list[TaskCreatePlan] = []
    errors: list[ValidationError] = []

    # Map line_number → list of child line_numbers (for D64 parent.depends_on).
    children_of: dict[int, list[int]] = {ln.line_number: [] for ln in lines}
    # Map line_number → parent line_number (for D65 sibling sequencing).
    parent_of: dict[int, int | None] = {ln.line_number: None for ln in lines}
    # Stack of (indent, line_number) tracking the open ancestor chain.
    stack: list[tuple[int, int]] = []

    for line in lines:
        # Pop ancestors that are at the same or deeper indent than the
        # current line — those can't be ancestors of `line`.
        while stack and stack[-1][0] >= line.indent:
            stack.pop()

        if stack:
            parent_ln = stack[-1][1]
            parent_of[line.line_number] = parent_ln
            children_of[parent_ln].append(line.line_number)

        stack.append((line.indent, line.line_number))

    # Per-parent sibling ordering. We need to look up by parent
    # line_number whether that parent's `is_sequential_parent` flag is
    # set, so build a fast lookup.
    line_by_number: dict[int, ParsedTaskLine] = {ln.line_number: ln for ln in lines}

    # D66: empty parent detection. A line that has children AND is_empty
    # is an empty-parent error. (An empty leaf is fine — it just becomes
    # a Lithos task with an empty description; today Lithos would
    # presumably reject it on its own, but the operator shouldn't write
    # one and we don't need to second-guess. The PRD specifically
    # targets the "empty parent reading as a heading" case.)
    for line in lines:
        if line.is_empty and children_of[line.line_number]:
            errors.append(
                ValidationError(
                    line_number=line.line_number,
                    kind="empty_parent",
                    message=(
                        f"line {line.line_number}: parent task has indented "
                        "children but its own description is empty (reads as a "
                        "heading) — flesh out the description or remove the line"
                    ),
                )
            )

    # Build per-task depends_on + parallelizable per D63 / D64 / D65.
    for line in lines:
        own_children = children_of[line.line_number]
        parent_ln = parent_of[line.line_number]

        # Depends_on: parents depend on children (D64). Children that
        # are siblings of a [sequential] parent ALSO depend on their
        # previous sibling (D65).
        depends_on: list[int] = list(own_children)

        if parent_ln is not None:
            parent_line = line_by_number[parent_ln]
            if parent_line.is_sequential_parent:
                siblings = children_of[parent_ln]
                idx = siblings.index(line.line_number)
                if idx > 0:
                    depends_on.append(siblings[idx - 1])

        # Parallelizable: True for siblings under a non-sequential
        # parent (D65 default). False for top-level tasks (D63 — they
        # have no parallelism contract), for parent tasks themselves
        # (they're gated on their children), and for siblings of a
        # [sequential] parent (they're explicitly serialized).
        parallelizable = False
        if parent_ln is not None:
            parent_line = line_by_number[parent_ln]
            if not parent_line.is_sequential_parent:
                parallelizable = True

        plans.append(
            TaskCreatePlan(
                line=line,
                depends_on_line_numbers=tuple(depends_on),
                parallelizable=parallelizable,
            )
        )

    return plans, errors
