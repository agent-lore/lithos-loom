"""``lithos-loom develop merge-gate`` — trial-merge a PR and gate the result (PRD S3).

The on-demand / subprocess face of :mod:`plugins.story_develop.merge_gate`:
resolve the PR the way ``review`` / ``converge`` do, merge its base's current
tip in a throwaway worktree, report the conflicting paths or run the current
check-set on the merge result, and push the merge commit onto the PR branch
when green and behind. Zero agent tokens — no coder, no panel — so there is
no model policy or acceptance-criteria brief here; the check-set flags are the
same ones ``converge`` takes.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from lithos_loom.cli.review import resolve_check_commands, resolve_check_states
from lithos_loom.config import load_config
from lithos_loom.plugins.story_develop.config import (
    DEFAULT_IMAGE,
    DEFAULT_TEST_TIMEOUT,
    DevelopConfig,
    parse_image,
    parse_parity_command,
    parse_test_command,
)
from lithos_loom.plugins.story_develop.merge_gate import MergeGateResult, run_merge_gate
from lithos_loom.plugins.story_develop.profiles import UnknownProfileError, get_profile
from lithos_loom.plugins.story_develop.review_resolve import resolve_change

__all__ = ["EXIT_CODES", "merge_gate_command"]

# The gate verdict decides the exit code; a green gate whose update lost a push
# race still exits 0 — the PR is mergeable as it stands, the push is advisory.
EXIT_CODES: dict[str, int] = {
    "green": 0,
    "no_checks": 0,
    "red": 1,
    "errored": 1,
    "conflict": 3,
    "fork_unsupported": 2,
}


def merge_gate_command(
    change: str = typer.Argument(
        ...,
        help="The PR to trial-merge: #142 / 142 / a GitHub PR URL.",
    ),
    profile: str = typer.Option(
        "standard",
        "--profile",
        "-p",
        help="Review Profile whose check-set gates the merge result.",
    ),
    check_command: list[str] | None = typer.Option(
        None,
        "--check-command",
        help="Override a check's command: NAME=COMMAND (repeatable).",
    ),
    check_state: list[str] | None = typer.Option(
        None,
        "--check-state",
        help="Override a check's blocking state: NAME=STATE (repeatable).",
    ),
    test_command: str | None = typer.Option(
        None,
        "--test-command",
        help="Explicit `test` check command (beats detection).",
    ),
    parity_command: str | None = typer.Option(
        None,
        "--parity-command",
        help="Repo-parity command run as a required raw-exit check.",
    ),
    image: str = typer.Option(
        DEFAULT_IMAGE,
        "--image",
        help="Sandbox image the checks run in.",
    ),
    test_timeout: int = typer.Option(
        DEFAULT_TEST_TIMEOUT,
        "--test-timeout",
        help="Per-check timeout in seconds.",
    ),
    no_push: bool = typer.Option(
        False,
        "--no-push",
        help="Gate only: never push the merge commit onto the PR branch.",
    ),
    keep_worktree: bool = typer.Option(
        False,
        "--keep-worktree",
        help="Keep the throwaway worktree (the merged tree) for inspection.",
    ),
    repo: Path | None = typer.Option(
        None, "--repo", help="Repository to work in (default: current directory)."
    ),
    json_out: Path | None = typer.Option(
        None, "--json", help="Write the structured JSON record to this path."
    ),
    config: Path | None = typer.Option(None, "--config", help="Host config path."),
) -> None:
    """Trial-merge a PR into its current base and run the check-set on the result."""
    try:
        get_profile(profile)
    except UnknownProfileError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if test_timeout < 1:
        raise typer.BadParameter("--test-timeout must be at least 1 second")
    check_commands = resolve_check_commands(check_command)
    check_states = resolve_check_states(check_state)
    try:
        test_command = parse_test_command(test_command, where="--test-command")
        parity_command = parse_parity_command(parity_command, where="--parity-command")
        resolved_image = parse_image(image, where="--image") or DEFAULT_IMAGE
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    repo = repo or Path.cwd()
    host = load_config(config)

    resolved = resolve_change(repo, change, base_branch="main")
    if not resolved.head_branch:
        raise typer.BadParameter(
            f"merge-gate takes a PR (it trial-merges the PR's base and may push "
            f"the update to the PR branch); {change!r} resolved to a range / "
            "branch with no PR."
        )

    develop_config = DevelopConfig(
        repo=repo,
        description=f"merge-gate {resolved.head_ref}",
        work_dir=host.orchestrator.work_dir / "merge-gate",
        review_profile=profile,
        test_command=test_command,
        test_timeout=test_timeout,
        check_commands=check_commands,
        check_states=check_states,
        parity_command=parity_command,
        image=resolved_image,
    )

    result = run_merge_gate(
        develop_config, resolved, push=not no_push, keep_worktree=keep_worktree
    )

    typer.echo(_render(result))
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(result.to_json(), indent=2), encoding="utf-8")
    raise typer.Exit(EXIT_CODES.get(result.status, 1))


def _render(result: MergeGateResult) -> str:
    lines = [f"merge-gate {result.change.head_ref}: {result.status}"]
    if result.message:
        lines.append(f"  {result.message}")
    if result.base_sha:
        lines.append(
            f"  base {result.base_ref} @ {result.base_sha[:12]}   head "
            f"{result.head_sha[:12]}   " + ("behind" if result.behind else "up to date")
        )
    for path in result.conflicting_paths:
        lines.append(f"  conflict: {path}")
    for c in result.checks:
        verdict = "pass" if c.passed else "FAIL"
        lines.append(f"  check {c.name} [{c.state}]: {c.outcome} → {verdict}")
    if result.pushed:
        lines.append(f"  pushed {result.pushed_sha[:12]} → {result.change.head_branch}")
    elif result.push_error:
        lines.append(f"  not pushed: {result.push_error}")
    if result.worktree is not None:
        lines.append(f"  worktree kept: {result.worktree}")
    return "\n".join(lines)
