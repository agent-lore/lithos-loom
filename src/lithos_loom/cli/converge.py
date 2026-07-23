"""``lithos-loom develop converge`` — converge an existing PR to review-green.

On-demand review-convergence loop (converge / ADR 0003 §9 "Shape 1"): resolve a
PR, run loom's in-container reviewer panel + deterministic gate against it, have
a coder fix the PR branch, re-review, and loop until the panel LGTMs **and** the
gate floor is clean — then fast-forward-push the fixed branch back to the PR
head, ready for the human merge gate. Exits 0 when the PR is review-green
(already-clean or converged), non-zero otherwise.

A thin wrapper over :func:`converge_pr`; it shares the intake + fix loop with
``develop review`` (same panel primitive) and story-develop (same ``develop()``
loop) — see :mod:`lithos_loom.plugins.story_develop.converge`. The acceptance-
criteria precedence and reviewer/profile resolution are the ``review`` command's,
reused verbatim (no second implementation).
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from lithos_loom.cli.review import resolve_acceptance_criteria, resolve_reviewers
from lithos_loom.config import load_config
from lithos_loom.plugins.story_develop import engines
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.converge import ConvergeResult, converge_pr
from lithos_loom.plugins.story_develop.profiles import UnknownProfileError, get_profile
from lithos_loom.plugins.story_develop.review_resolve import resolve_change

# status -> process exit code. Review-green (nothing left for the operator to do)
# is 0; a bad-input refusal (fork) is 2; everything else that needs a human is 1.
_EXIT_CODES = {
    "already_clean": 0,
    "converged": 0,
    "fork_unsupported": 2,
    "not_converged": 1,
    "merge_race": 1,
    "failed": 1,
}


def converge_command(
    change: str = typer.Argument(
        ...,
        help="The PR to converge: #142 / 142 / a GitHub PR URL. "
        "converge pushes fixes to the PR branch, so a bare range / branch is rejected.",
    ),
    profile: str = typer.Option(
        "standard",
        "--profile",
        "-p",
        help="Review profile (selects panel + check-set).",
    ),
    reviewer: list[str] = typer.Option(
        None, "--reviewer", help="Override the panel personas (repeatable)."
    ),
    acceptance: str | None = typer.Option(
        None, "--ac", help="Acceptance criteria text (the PR's intent)."
    ),
    acceptance_file: Path | None = typer.Option(
        None, "--ac-file", help="Read acceptance criteria from a file."
    ),
    base: str | None = typer.Option(
        None, "--base", help="Override the diff base (default: the PR merge-base)."
    ),
    coder: str | None = typer.Option(
        None, "--coder", help="Coder engine for the fix turns (claude / codex)."
    ),
    max_rounds: int | None = typer.Option(
        None, "--max-rounds", help="Cap the implement→review→fix rounds."
    ),
    max_cost: float | None = typer.Option(
        None, "--max-cost", help="Stop once total agent spend (USD) exceeds this."
    ),
    no_push: bool = typer.Option(
        False, "--no-push", help="Converge locally but do not push to the PR branch."
    ),
    repo: Path | None = typer.Option(
        None, "--repo", help="Repository to converge in (default: current directory)."
    ),
    json_out: Path | None = typer.Option(
        None, "--json", help="Write the structured JSON summary to this path."
    ),
    config: Path | None = typer.Option(None, "--config", help="Host config path."),
) -> None:
    """Converge an existing PR to review-green (panel + gate), then push."""
    # Fail closed on an unknown profile / coder before spending any containers,
    # through the same single known-set seams the rest of the code uses.
    try:
        get_profile(profile)
    except UnknownProfileError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if coder is not None and not engines.is_supported(coder):
        raise typer.BadParameter(
            f"unsupported coder {coder!r}: expected {engines.supported_tools_phrase()}"
        )

    repo = repo or Path.cwd()
    host = load_config(config)

    resolved = resolve_change(repo, change, base_branch="main", base_override=base)

    # converge pushes fixes onto the PR head ref, so it needs a PR (a range /
    # branch spec has no pushable head branch). Reject those up front.
    if not resolved.head_branch:
        raise typer.BadParameter(
            f"converge requires a PR (it pushes fixes to the PR branch); "
            f"{change!r} resolved to a range / branch with no pushable head. "
            "Use `develop review` for a read-only review of an arbitrary range."
        )

    criteria = resolve_acceptance_criteria(acceptance, acceptance_file, resolved.body)
    if not criteria:
        typer.secho(
            "error: no acceptance criteria for the converge run — pass --ac / "
            "--ac-file (a PR's body is used automatically, but this PR has none).",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    reviewers = resolve_reviewers(profile, reviewer)

    overrides: dict = {}
    if coder is not None:
        overrides["coder"] = coder
    if max_rounds is not None:
        overrides["max_rounds"] = max_rounds

    develop_config = DevelopConfig(
        repo=repo,
        description=resolved.title or f"Converge {resolved.head_ref}",
        work_dir=host.orchestrator.work_dir / "converge",
        acceptance_criteria=criteria,
        review_profile=profile,
        reviewers=reviewers,
        base_branch=base or "main",
        max_cost_usd=max_cost,
        **overrides,
    )

    result = converge_pr(develop_config, resolved, no_push=no_push)

    typer.echo(_render(result))
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(result.to_json(), indent=2), encoding="utf-8")

    raise typer.Exit(_EXIT_CODES.get(result.status, 1))


def _render(result: ConvergeResult) -> str:
    """Human-readable one-block summary of the converge outcome."""
    change = result.change
    lines = [f"converge {change.head_ref}: {result.status}"]
    if result.message:
        lines.append(f"  {result.message}")
    dev = result.develop_result
    if dev is not None:
        lines.append(
            f"  rounds: {dev.rounds}   fixer commits: {len(result.fixer_commits)}"
        )
    if result.pushed:
        lines.append(f"  pushed {result.pushed_sha[:10]} → {change.head_branch}")
    return "\n".join(lines)
