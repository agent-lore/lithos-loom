"""Reporting a checkout refusal before external remediation is dispatched."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lithos_loom.gates import PrGateSpec
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import post_finding_then_mark

# ── a mis-mapped checkout (PR #362 re-review 2 F2) ─────────────────────────

# Gate-metadata key de-duping the mis-mapped-checkout friction: {pr_url,
# repo_path, actual_repo}. The refusal itself spends nothing and re-checks
# every sweep; only the story finding is one-shot per mismatch.
REPO_MISMATCH_KEY = "external_remediation_repo_mismatch"


def refusal_key(spec: PrGateSpec, repo: Path, origin_seen: str) -> dict[str, str]:
    """What the sweep observes about the mapped checkout — the settle key a
    repo-mismatch refusal (the sweep's own, or the CLI's) is de-duped on."""
    return {"pr_url": spec.pr_url, "repo_path": str(repo), "origin_seen": origin_seen}


def settled_refusal(
    gate: Any, spec: PrGateSpec, repo: Path, origin_seen: str
) -> str | None:
    """The kind of refusal (``repo_mismatch`` / ``checkout_unresolved``)
    already recorded for exactly this key — no spawn, no re-post until the
    mapping or the read moves — or ``None``."""
    raw = gate.metadata.get(REPO_MISMATCH_KEY)
    if not isinstance(raw, dict):
        return None
    key = refusal_key(spec, repo, origin_seen)
    if not all(raw.get(k) == v for k, v in key.items()):
        return None
    kind = raw.get("kind")
    return kind if isinstance(kind, str) and kind else "repo_mismatch"


async def post_checkout_unresolved_refusal(
    ctx: SubscriptionContext,
    *,
    gate: Any,
    story_id: str,
    spec: PrGateSpec,
    repo: Path,
    reason: str,
) -> None:
    """The sweep could not resolve the mapped checkout's origin (PR #362
    re-review 3 F1): no spawn, no round spent, the parked trigger kept, one
    ``[Friction]`` naming why; settled on (path, "") until the path changes
    or the read starts to answer."""
    ctx.logger.warning(
        "[Friction] external-remediation: checkout %s cannot be resolved (%s); "
        "not dispatching for %s (the parked trigger, if any, waits)",
        repo,
        reason,
        spec.pr_url,
    )
    await post_finding_then_mark(
        ctx,
        task_id=story_id,
        summary=(
            f"[Friction] external-remediation: the checkout mapped for this "
            f"project ({repo}) cannot be resolved ({reason}: not a git checkout, "
            f"no origin remote, or an origin that is not a GitHub url); no "
            f"converge was dispatched and no budget round spent (PR "
            f"{spec.pr_url}). Provision the checkout with origin {spec.repo}, or "
            f"fix [projects.<slug>].repo and restart loom — the parked review "
            f"trigger resumes then."
        ),
        marker={
            REPO_MISMATCH_KEY: {
                **refusal_key(spec, repo, ""),
                "kind": "checkout_unresolved",
                "actual_repo": f"unresolved:{reason}",
            }
        },
        subsystem="external-remediation",
        retry_hint="will retry next sweep",
        marker_task_id=gate.id,
    )


async def post_repo_mismatch_refusal(
    ctx: SubscriptionContext,
    *,
    gate: Any,
    story_id: str,
    spec: PrGateSpec,
    repo: Path,
    origin: str,
) -> None:
    """The sweep's own origin read refused the checkout: one ``[Friction]``
    on the story per settle key, de-duped by a marker on the gate. Nothing
    else is written — no round spent, the parked trigger kept."""
    current = {
        **refusal_key(spec, repo, origin.lower()),
        "kind": "repo_mismatch",
        "actual_repo": origin,
    }
    ctx.logger.warning(
        "[Friction] external-remediation: checkout %s has origin %s, not the "
        "gate's %s; not dispatching for %s (the parked trigger, if any, waits)",
        repo,
        origin,
        spec.repo,
        spec.pr_url,
    )
    await post_finding_then_mark(
        ctx,
        task_id=story_id,
        summary=(
            f"[Friction] external-remediation: the checkout mapped for this "
            f"project ({repo}) has origin {origin}, not the gate's {spec.repo} "
            f"(PR {spec.pr_url}); no converge was dispatched and no budget "
            f"round spent. Fix [projects.<slug>].repo in the host config and "
            f"restart loom — the parked review trigger resumes then."
        ),
        marker={REPO_MISMATCH_KEY: current},
        subsystem="external-remediation",
        retry_hint="will retry next sweep",
        marker_task_id=gate.id,
    )
