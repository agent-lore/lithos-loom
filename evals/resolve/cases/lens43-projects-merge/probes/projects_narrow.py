"""Oracle probe for lens43-projects-merge: a board narrowed ONLY by
``?project=`` must count as narrowed, so the dashboard withholds its
whole-system claims (the "All systems healthy" stripe, the empty-corpus
panel) — the term the operator's resolution added (lens e1965aa) and the
plausible wrong merge lacks.

Run with cwd = the tree under test (``uv run`` builds that tree's own env).
Location-agnostic on purpose: the delivered head keeps the helper in
``tasks.py``, the operator's merge moved it into ``task_filtering.py`` (the
module T1-S9 introduced on the base), and a resolution may leave it in
either — the oracle asks what the helper DOES, not where it lives.
Exit 0 = holds; 1 = the property fails; 2 = the helper is not found (a
resolution that dropped it altogether also fails the oracle).
"""

# The import below is the LENS tree's package: this runs under that tree's
# env, never loom's, so loom's own typecheck cannot resolve it.
# pyright: reportMissingImports=false
from __future__ import annotations

import importlib
import sys

from lithos_lens.tasks import TASK_STATUSES, TaskFilters

helper = None
for module in (
    "lithos_lens.task_filtering",
    "lithos_lens.tasks",
    "lithos_lens.frontier",
):
    try:
        helper = getattr(
            importlib.import_module(module), "filters_narrow_the_board", None
        )
    except ImportError:
        helper = None
    if helper is not None:
        break
if helper is None:
    print("probe: filters_narrow_the_board not found in any known module")
    sys.exit(2)

# every other filter at its default: statuses = the full set, no tag, no agent
filters = TaskFilters(
    statuses=tuple(TASK_STATUSES), tags=(), agent="", since="", projects=("influx",)
)
narrowed = bool(helper(filters))
print(
    f"probe: {helper.__module__}.filters_narrow_the_board(projects-only) = {narrowed}"
)
sys.exit(0 if narrowed else 1)
