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
    from .check_set import CheckResult, CheckSetResult
    from .config import DevelopConfig
    from .panel import ReviewOutcome

__all__ = ["LoopEntry", "PostCommitOutcome"]


@dataclass(frozen=True)
class PostCommitOutcome:
    """What a round's post-commit pass left behind (PRD S4; PR #388 review).

    ``sha`` is the commit the pass made — the gated tree moves to it. ``row``
    is the pass's verdict as a check-set result riding with the round's
    commit: a REQUIRED raw-exit check, so a red one holds approval through
    the floor, reaches the coder's next prompt with its output tail and is
    named in the run outcome, while a green one tells the panel the rebuild
    ran clean. A pass that ran its tool returns a row EVERY time (green
    included), so a prior commit's row never outlives the commit it
    described; a round with no new commit keeps the prior row (the tree is
    unchanged). ``infra_error`` means the pass never got a verdict (export /
    container runtime) — nothing the coder can fix — and the round is
    terminal ``infra_failed`` with ``host_action``. The empty outcome is
    reserved for a pass with nothing to run.
    """

    sha: str | None = None
    row: CheckResult | None = None
    infra_error: str = ""
    host_action: str = ""


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
    # PRD S4: a pass run after every round commit (and the auto-format pass),
    # ``(worktree, round_no) -> PostCommitOutcome`` — resolve mode regenerates
    # the project's generated paths deterministically and commits the result,
    # so the gate + panel never judge a hand-merged or stale generated file;
    # its verdict rides with the commit as a required check row, and a pass
    # that cannot run ends the round ``infra_failed`` (fail closed).
    post_commit_pass: Callable[[Path, int], PostCommitOutcome] | None = None
    # ...and the panel's merge-shaped context (PR #364 review F1): the
    # conflicted paths and both parents, so a reviewer can see a resolution
    # that took the base version — invisible in the fork-point diff.
    review_context: str = ""
    # External mode (PR #396 review): ``(round_no) -> bool`` — whether the
    # round's coder handoff claims that EVERY injected finding needs no
    # change. Round 1 must otherwise commit (exit C); a true claim admits the
    # empty round instead as a VALIDATION pass: the loop's own gate + panel
    # judge it at the unchanged head — approval seals the run, rejection ends
    # it with the rationale (never an entry to the fix loop) — the external
    # reviewer proposes, the loop gate disposes, never the coder alone. None
    # (the default) keeps exit C.
    no_change_claim: Callable[[int], bool] | None = None
