"""``LoopEntry`` — how ``develop()`` enters its loop on an EXISTING branch.

Split out of :mod:`.rounds` (the round pipeline sits at the module budget) so
the converge modes — local-panel intake, external injection (PRD S2) and
conflict resolution (PRD S5) — share one entry contract with no round code.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ...runner import git

if TYPE_CHECKING:
    from .check_set import CheckSetResult
    from .config import DevelopConfig
    from .panel import ReviewOutcome

__all__ = ["LoopEntry"]


@dataclass(frozen=True)
class LoopEntry:
    """Overrides that let ``develop()`` enter its loop on an EXISTING PR branch
    instead of cutting a fresh worktree off a base (converge / ADR 0003 §9
    "Shape 1").

    ``worktree_factory`` builds the committable worktree — converge positions a
    fresh local branch at the PR head so the coder's commits land on it and can
    be pushed. ``base_override`` is the PR's merge-base (the review + gate
    diff base, not the worktree HEAD) paired with the live base ref, so a base
    merge during the run moves the fork point (S5c). ``intake_reviews`` /
    ``intake_check_set``
    seed round 1's cold-start coder from the intake review of the PR (there is no
    prior coder session to resume — converge is a fresh process). The default
    ``entry=None`` on ``develop()`` is the story-develop path, unchanged.
    """

    worktree_factory: Callable[[DevelopConfig], Path]
    base_override: git.RangeBase
    intake_reviews: list[ReviewOutcome]
    intake_check_set: CheckSetResult | None
    # External mode (PRD S2): the per-id acknowledgement contract block for
    # the round-1 coder prompt's `{external_ack}` slot. Empty (the default)
    # renders nothing — the local-panel converge path is unchanged.
    external_ack: str = ""
    # Conflict resolution (PRD S5): round 1 renders THIS template instead of
    # converge_coder_init.md, with ``coder_init_extra`` as additional slots
    # (the conflict brief), and ``pre_commit_guard`` runs before every round
    # commit — a non-None return is the round's failure reason and nothing is
    # committed (a tree still carrying conflict markers must never be gated,
    # reviewed or pushed).
    coder_init_template: str = "converge_coder_init.md"
    coder_init_extra: Mapping[str, str] = field(default_factory=dict)
    pre_commit_guard: Callable[[Path], str | None] | None = None
