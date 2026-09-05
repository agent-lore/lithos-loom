"""PR landability for still-open ``pr`` gates (PRD S1, detection).

A delivered PR waits behind its ``pr`` gate for a human merge. Until now the
sweep asked GitHub one question about it — merged yet? — and a PR that had
drifted into conflict with its base sat there indefinitely with nothing on
the story to say so (the 2026-08-22 lens rollout, and loom#352 on
2026-09-05). This module is S1: each sweep of a still-open gate reads the
landability GitHub already returns on the same ``GET /pulls/{n}`` and posts
a one-shot ``[PRConflicted]`` finding on the blocked *story* when the PR
cannot merge, with a de-dup marker on the *gate*.

Two caveats, from the PRD, that shape the code:

1. GitHub computes ``mergeable`` lazily and returns ``null`` on a cold fetch.
   ``null`` is "ask again" — the sweep writes nothing and re-asks next pass;
   it is never read as clean.
2. The marker key is ``(pr_url, base_sha, head_sha)``, not ``(pr_url,
   base_sha)``: pushing a conflict resolution moves the **head** without
   moving the base, so a base-scoped marker would suppress the re-check that
   proves the fix landed. Same shape as ``develop_pr_merge_state``, one
   field wider.

What S1 alone cannot say: **which paths conflict.** The API reports
``mergeable_state: dirty`` and nothing more; the path list comes from S3's
trial merge, which reads this marker and widens the report. Until S3 lands
the finding names the base branch and the two shas and leaves the resolution
to the operator (merge the base into the branch; never rebase a delivered
branch — ADR 0011 decision 4).
"""

from __future__ import annotations

from typing import Any

from lithos_loom.gates import PrGateSpec
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import post_finding_then_mark, write_marker

__all__ = [
    "LANDABILITY_KEY",
    "PR_CONFLICTED",
    "check_landability",
    "classify_landability",
]

# Stable, machine-parseable finding prefix (see AGENTS.md): a delivered PR
# still awaiting merge behind its `pr` gate cannot be merged as it stands —
# GitHub reports a conflict with its base.
PR_CONFLICTED = "[PRConflicted]"

# Gate-metadata key holding the last landability observation:
# {"pr_url", "base_sha", "head_sha", "state", "mergeable_state"}. pr_url scopes
# it (a replacement PR re-evaluates); the two shas are the de-dup key; state
# is `dirty` | `mergeable`; mergeable_state is GitHub's own word, kept for the
# operator (`behind`, `blocked`, `unstable` … are all `mergeable` here).
LANDABILITY_KEY = "pr_landability"


def classify_landability(pr: Any) -> str:
    """``unknown`` / ``dirty`` / ``mergeable`` from the fetched PR.

    A pure function of the ``GET /pulls/{n}`` fields: ``mergeable is None``
    is GitHub still computing (ask again); ``dirty`` in either field is a
    conflict (``mergeable`` is the verdict, ``mergeable_state`` the reason —
    trust whichever says so); everything else GitHub can merge. ``behind``,
    ``blocked``, ``unstable`` and ``has_hooks`` are not conflicts — the base
    moved, or checks/reviews are outstanding — and S1 reports conflicts only.
    """
    mergeable = getattr(pr, "mergeable", None)
    state = getattr(pr, "mergeable_state", "") or ""
    if state == "dirty" or mergeable is False:
        return "dirty"
    if mergeable is None:
        return "unknown"
    return "mergeable"


def _observed(gate: Any, pr_url: str) -> dict[str, Any] | None:
    raw = gate.metadata.get(LANDABILITY_KEY)
    if not isinstance(raw, dict) or raw.get("pr_url") != pr_url:
        return None
    return raw


async def check_landability(
    gate: Any,
    spec: PrGateSpec,
    story_id: str | None,
    pr: Any,
    ctx: SubscriptionContext,
) -> str:
    """Classify one still-open gate's PR and report a conflict once per
    ``(pr_url, base_sha, head_sha)``. Returns the state label. Never raises.

    ``unknown`` writes nothing (re-asked next sweep). ``dirty`` posts
    ``[PRConflicted]`` on the story — finding-then-mark, so a crash between
    the two costs at most one duplicate — unless the same shas were already
    reported. ``mergeable`` just records the observation (a resolved conflict
    is visible as the marker flipping; the story gets no "resolved" chatter —
    the merge itself is the event that matters, and the gate reports that).
    """
    state = classify_landability(pr)
    if state == "unknown":
        ctx.logger.debug(
            "pr-landability: %s mergeability not yet computed by GitHub; "
            "re-asking next sweep",
            spec.pr_url,
        )
        return state
    head_sha = getattr(pr, "head_sha", "") or ""
    base_sha = getattr(pr, "base_sha", "") or ""
    marker = {
        LANDABILITY_KEY: {
            "pr_url": spec.pr_url,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "state": state,
            "mergeable_state": getattr(pr, "mergeable_state", "") or "",
        }
    }
    prior = _observed(gate, spec.pr_url)
    if (
        prior is not None
        and prior.get("base_sha") == base_sha
        and prior.get("head_sha") == head_sha
        and prior.get("state") == state
    ):
        return state  # already observed at exactly these shas — nothing new

    if state == "mergeable":
        if prior is not None and prior.get("state") == "dirty":
            ctx.logger.info(
                "pr-landability: %s is mergeable again at head %s (was dirty at %s)",
                spec.pr_url,
                head_sha[:12],
                str(prior.get("head_sha", ""))[:12],
            )
        await write_marker(
            ctx, task_id=gate.id, marker=marker, subsystem="pr-landability"
        )
        return state

    base_ref = getattr(pr, "base_ref", "") or "the base branch"
    if story_id is None:
        await write_marker(
            ctx, task_id=gate.id, marker=marker, subsystem="pr-landability"
        )
        ctx.logger.warning(
            "[Friction] pr-landability: gate %s has no waiter; PR %s conflicts "
            "with %s (recorded, not posted)",
            gate.id,
            spec.pr_url,
            base_ref,
        )
        return state
    await post_finding_then_mark(
        ctx,
        task_id=story_id,
        summary=(
            f"{PR_CONFLICTED} delivered PR {spec.pr_url} cannot be merged: GitHub "
            f"reports a conflict with {base_ref} (head {head_sha[:12]} against "
            f"base {base_sha[:12]}); story {story_id} remains blocked on gate "
            f"{gate.id}. The API does not name the conflicting paths. Resolve by "
            f"merging {base_ref} into the PR branch (never rebase a delivered "
            f"branch) and pushing; the next sweep re-evaluates at the new head."
        ),
        marker=marker,
        subsystem="pr-landability",
        retry_hint="will retry next sweep",
        marker_task_id=gate.id,
    )
    ctx.logger.info(
        "pr-landability: posted %s for %s (head %s, base %s) on story %s",
        PR_CONFLICTED,
        spec.pr_url,
        head_sha[:12],
        base_sha[:12],
        story_id,
    )
    return state
