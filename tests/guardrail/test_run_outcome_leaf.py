"""Guard: ``run_outcome`` stays a leaf — stdlib + ``.idempotency`` only.

``story_develop.run_outcome`` owns the develop-run on-disk contract and is
imported by BOTH the reader (``cli/develop.py``) and, in a later slice, the
writer side. Its value is being importable to *classify a run* without dragging
in the plugin runtime (develop / pr_delivery / containers / turns) or the config
/ client stack. The import-linter contract in ``pyproject.toml`` is the denylist
half (it also catches indirect paths through ``.idempotency``); this test is the
literal half — a denylist can't name every module a future edit might reach for,
so we assert directly that run_outcome's OWN imports are stdlib plus the single
allowed internal edge, ``.idempotency``. A new ``from .handoff import …`` (or any
other internal import) fails here.
"""

from __future__ import annotations

import ast
from pathlib import Path

# run_outcome lives at this dotted path; its package is the parent of that.
_MODULE = "lithos_loom.plugins.story_develop.run_outcome"
_PACKAGE = _MODULE.rsplit(".", 1)[0]
# The only import under lithos_loom that run_outcome is allowed to make.
_ALLOWED_INTERNAL = {"lithos_loom.plugins.story_develop.idempotency"}

_RUN_OUTCOME_PY = (
    Path(__file__).resolve().parents[2]
    / "src/lithos_loom/plugins/story_develop/run_outcome.py"
)


def _import_targets(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Absolute module names an import node pulls in (resolving relative ones)."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    # ast.ImportFrom
    if node.level == 0:
        return [node.module] if node.module else []
    # relative: drop (level - 1) trailing components off the package to get the base
    parts = _PACKAGE.split(".")
    base = ".".join(parts[: len(parts) - (node.level - 1)])
    if node.module:  # `from .idempotency import x` → base.idempotency
        return [f"{base}.{node.module}"]
    # `from . import handoff` → base.handoff (each name is a submodule)
    return [f"{base}.{alias.name}" for alias in node.names]


def _is_internal(target: str) -> bool:
    return target == "lithos_loom" or target.startswith("lithos_loom.")


def test_run_outcome_is_a_leaf_module() -> None:
    tree = ast.parse(_RUN_OUTCOME_PY.read_text(encoding="utf-8"), str(_RUN_OUTCOME_PY))
    internal = [
        target
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for target in _import_targets(node)
        if _is_internal(target)
    ]
    offending = sorted(t for t in internal if t not in _ALLOWED_INTERNAL)
    assert not offending, (
        "run_outcome must import only stdlib + .idempotency, but also imports: "
        + ", ".join(offending)
    )
    # sanity: the one allowed internal edge is actually present (guards against a
    # future refactor that quietly drops it, leaving the test vacuously green).
    assert _ALLOWED_INTERNAL.issubset(set(internal))
