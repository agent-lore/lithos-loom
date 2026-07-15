"""Pure task-graph builder for bulk task import.

Takes the list of ``ParsedTaskLine`` produced by ``task_line_parser``
and returns ``TaskCreatePlan`` entries describing the graph the
indentation implies (Epic G / US9):

- A line with indented children is an **epic** (``task_type="epic"``) and
  its children carry ``parent_task_id`` — a first-class ``parent_child``
  edge. Containment is now structural, so an epic is never dispatched as
  work (``lithos_task_ready`` excludes epics by construction) and no
  longer needs to fake "wait for my children" with dependency edges.
- Sibling children are parallel by default — *absence* of a ``blocks``
  edge is what parallelism means now, so nothing is written for it. When
  the parent carries the ``[sequential]`` marker
  (``is_sequential_parent = True``) its children form a chain instead:
  child[i] ``depends_on`` child[i-1], one ``blocks`` edge each.
- Top-level tasks are flat: no parent, no dependencies.
- Parent tasks with empty descriptions (just a heading) are flagged as
  validation errors.

This replaces an older encoding that had **the parent depend on its
children** via ``metadata.depends_on``, plus ``metadata.parallelizable``.
That was a workaround for a Lithos with no edge surface: containment had
to be spelled as a dependency. It is gone for two reasons — Lithos now
*rejects* ``metadata.depends_on`` outright (``invalid_metadata_key``:
"task dependencies are first-class task edges"), and the parent→child
dependency inverted the real relationship.

Because a parent always precedes its children in the document and
siblings appear in order, **document order is a valid creation order**:
every ``parent_task_id`` and ``depends_on`` reference points at a line
that was already created. No topological sort is needed.

The builder is I/O-free. Line-number references are resolved to Lithos
task ids at execution time by the CLI layer.
"""

from __future__ import annotations

from dataclasses import dataclass

from lithos_loom.task_line_parser import ParsedTaskLine, ValidationError

EPIC = "epic"
TASK = "task"


@dataclass(frozen=True)
class TaskCreatePlan:
    """One Lithos ``task_create`` call's worth of structured input.

    ``parent_line_number`` / ``depends_on_line_numbers`` reference other
    ``ParsedTaskLine``s by their ``line_number``; the CLI layer creates
    tasks in document order and resolves each to a freshly-minted Lithos
    task id by the time it is referenced.

    ``task_type`` is ``epic`` for a line with children (a container, not
    work) and ``task`` otherwise. Lithos has no ``subtask`` type — a child
    is a plain ``task`` that carries a ``parent_task_id``.
    """

    line: ParsedTaskLine
    task_type: str
    parent_line_number: int | None
    depends_on_line_numbers: tuple[int, ...]


def build_plan(
    lines: list[ParsedTaskLine],
) -> tuple[list[TaskCreatePlan], list[ValidationError]]:
    """Build task-create plans with graph edges derived from indentation.

    Walks ``lines`` in document order, tracking a stack of open
    ancestors. A line whose indent is deeper than the top of the
    stack is a child of the top. A line whose indent is the same as
    or shallower than the top pops the stack until it would be a
    valid child (or the stack is empty, meaning it's a top-level
    task).

    Returns:
        (plans, errors). ``plans`` preserves the input order, which is
        also a valid creation order — see the module docstring.
        ``errors`` contains empty-parent violations only —
        cross-project tag violations come from the parser itself and
        are passed through the CLI separately.
    """
    plans: list[TaskCreatePlan] = []
    errors: list[ValidationError] = []

    # Map line_number → list of child line_numbers, in document order.
    children_of: dict[int, list[int]] = {ln.line_number: [] for ln in lines}
    # Map line_number → parent line_number (for sibling sequencing).
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

    # Empty parent detection. A line that has children AND is_empty
    # is an empty-parent error. (An empty leaf is fine — it just becomes
    # a Lithos task with an empty description; today Lithos would
    # presumably reject it on its own, but the operator shouldn't write
    # one and we don't need to second-guess. The validated case is the
    # "empty parent reading as a heading" anti-pattern.)
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

    # Build per-task type / parent / dependencies.
    for line in lines:
        parent_ln = parent_of[line.line_number]

        # A line with children is a container, not a unit of work.
        task_type = EPIC if children_of[line.line_number] else TASK

        # Dependencies: only the [sequential] sibling chain. Containment is
        # `parent_task_id`, and parallel siblings are the *absence* of edges.
        depends_on: list[int] = []
        if parent_ln is not None and line_by_number[parent_ln].is_sequential_parent:
            siblings = children_of[parent_ln]
            idx = siblings.index(line.line_number)
            if idx > 0:
                depends_on.append(siblings[idx - 1])

        plans.append(
            TaskCreatePlan(
                line=line,
                task_type=task_type,
                parent_line_number=parent_ln,
                depends_on_line_numbers=tuple(depends_on),
            )
        )

    return plans, errors
