"""Guard: a name imported under ``if TYPE_CHECKING:`` is never used at runtime.

Neither pyright nor ruff catches this class: the checker sees the name as
defined, and the interpreter raises ``NameError`` only when the line runs. The
whole hermetic gate missed one (``isinstance(meta, Mapping)`` in
``daemon_io.fetch_task_metadata``, #361) because every test stubbed the
function — the live daemon then crashed every ``--story`` run. This walks every
module under ``src/lithos_loom`` and flags a type-only name loaded anywhere
outside an annotation slot (with ``from __future__ import annotations``
annotations are lazy; without it they are runtime too).
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "lithos_loom"


def _type_only_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_tc = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if not is_tc:
            continue
        for stmt in node.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                for alias in stmt.names:
                    names.add((alias.asname or alias.name).split(".")[0])
    return names


def _annotation_nodes(tree: ast.Module) -> set[int]:
    """ids of every node inside an annotation slot (lazy under the future import)."""
    slots: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                slots.append(node.returns)
            args = node.args
            for arg in (
                *args.posonlyargs,
                *args.args,
                *args.kwonlyargs,
                *([args.vararg] if args.vararg else []),
                *([args.kwarg] if args.kwarg else []),
            ):
                if arg.annotation is not None:
                    slots.append(arg.annotation)
        elif isinstance(node, ast.AnnAssign):
            slots.append(node.annotation)
    return {id(n) for slot in slots for n in ast.walk(slot)}


def _has_lazy_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(a.name == "annotations" for a in node.names)
        for node in tree.body
    )


def _label(path: Path) -> str:
    if path.is_relative_to(_SRC):
        return str(path.relative_to(_SRC.parent))
    return path.name


def _runtime_uses(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    names = _type_only_names(tree)
    if not names:
        return []
    lazy = _annotation_nodes(tree) if _has_lazy_annotations(tree) else set()
    # the TYPE_CHECKING block itself binds the names; skip its own imports
    return [
        f"{_label(path)}:{node.lineno} {node.id}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in names
        and id(node) not in lazy
    ]


def test_type_checking_only_imports_are_never_used_at_runtime() -> None:
    offenders = [
        use for path in sorted(_SRC.rglob("*.py")) for use in _runtime_uses(path)
    ]
    assert offenders == [], "type-only names used at runtime:\n" + "\n".join(offenders)


def test_guard_flags_a_runtime_isinstance(tmp_path: Path) -> None:
    """The guard's own negative: the exact shape that escaped."""
    src = tmp_path / "m.py"
    src.write_text(
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from collections.abc import Mapping\n"
        "def f(x: Mapping[str, int]) -> Mapping[str, int]:\n"
        "    return x if isinstance(x, Mapping) else {}\n"
    )
    assert [u.split()[-1] for u in _runtime_uses(src)] == ["Mapping"]
