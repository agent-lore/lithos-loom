"""Unit tests for ``is_human_actionable`` (Slice 1 US8).

The function is pure: ``(Task, routes, ObsidianSyncConfig) -> bool``.
All tests construct minimal fixtures and assert the decision directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lithos_loom.config import ObsidianSyncConfig, RouteConfig, RouteMatch
from lithos_loom.lithos_client import Task
from lithos_loom.subscriptions._human_actionable import is_human_actionable


def _task(
    *,
    tags: tuple[str, ...] = (),
    metadata: Mapping[str, Any] | None = None,
) -> Task:
    return Task(
        id="t1",
        title="t",
        status="open",
        tags=tags,
        metadata=metadata or {},
        claims=(),
    )


def _route(name: str, *, tags: tuple[str, ...], human_blocking: bool) -> RouteConfig:
    return RouteConfig(
        name=name,
        command="echo hi",
        match=RouteMatch(tags=tags),
        human_blocking=human_blocking,
    )


def _cfg(
    *,
    include_blocked: bool = True,
    exclude_tags: tuple[str, ...] = (),
) -> ObsidianSyncConfig:
    return ObsidianSyncConfig(
        vault_path=Path("/vault"),
        include_blocked=include_blocked,
        exclude_tags=exclude_tags,
    )


def test_orphan_task_no_matching_route_returns_true() -> None:
    """A task with tags that no route consumes is the operator's problem."""
    routes = [_route("r1", tags=("trigger:x",), human_blocking=False)]
    task = _task(tags=("needs-review",))
    assert is_human_actionable(task, routes, _cfg()) is True


def test_matching_route_with_human_blocking_true_returns_true() -> None:
    routes = [_route("review", tags=("trigger:review",), human_blocking=True)]
    task = _task(tags=("trigger:review",))
    assert is_human_actionable(task, routes, _cfg()) is True


def test_matching_route_with_human_blocking_false_returns_false() -> None:
    """Autonomous handling — hide from operator."""
    routes = [_route("auto", tags=("trigger:auto",), human_blocking=False)]
    task = _task(tags=("trigger:auto",))
    assert is_human_actionable(task, routes, _cfg()) is False


def test_multiple_matching_routes_any_human_blocking_returns_true() -> None:
    """One human, one autonomous → operator-visible (any-blocking wins)."""
    routes = [
        _route("auto", tags=("trigger:shared",), human_blocking=False),
        _route("review", tags=("trigger:shared",), human_blocking=True),
    ]
    task = _task(tags=("trigger:shared",))
    assert is_human_actionable(task, routes, _cfg()) is True


def test_multiple_matching_routes_all_autonomous_returns_false() -> None:
    routes = [
        _route("auto1", tags=("trigger:shared",), human_blocking=False),
        _route("auto2", tags=("trigger:shared",), human_blocking=False),
    ]
    task = _task(tags=("trigger:shared",))
    assert is_human_actionable(task, routes, _cfg()) is False


def test_include_blocked_false_with_deps_returns_false() -> None:
    """Operator opted out of blocked work — even an orphan blocked task is hidden."""
    task = _task(tags=(), metadata={"depends_on": ["other-task-id"]})
    assert (
        is_human_actionable(task, routes=[], cfg=_cfg(include_blocked=False)) is False
    )


def test_include_blocked_true_with_deps_returns_true() -> None:
    """D6 revised: blocked tasks still project by default."""
    task = _task(tags=(), metadata={"depends_on": ["other-task-id"]})
    assert is_human_actionable(task, routes=[], cfg=_cfg(include_blocked=True)) is True


def test_excluded_tag_returns_false_even_for_orphan_task() -> None:
    """Operator denylist wins over the default-true orphan path."""
    task = _task(tags=("debug:trace", "needs-review"))
    cfg = _cfg(exclude_tags=("debug:trace",))
    assert is_human_actionable(task, routes=[], cfg=cfg) is False


def test_depends_on_missing_or_empty_does_not_block() -> None:
    """metadata.depends_on absent OR [] is not 'blocked' — both must project."""
    no_meta = _task(tags=(), metadata={})
    empty_deps = _task(tags=(), metadata={"depends_on": []})
    cfg = _cfg(include_blocked=False)  # the strictest setting
    assert is_human_actionable(no_meta, routes=[], cfg=cfg) is True
    assert is_human_actionable(empty_deps, routes=[], cfg=cfg) is True
