"""CLI sub-apps for the Slice 3 capture-macro surface (and future
operator surfaces). Kept separate from :mod:`lithos_loom.main` so
``main.py`` stays scannable as more subcommand groups land."""

from __future__ import annotations

from lithos_loom.cli.project import project_app
from lithos_loom.cli.task import task_app

__all__ = ["project_app", "task_app"]
