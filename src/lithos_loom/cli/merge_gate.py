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

from lithos_loom.cli.review import (
    StorySettingsUnresolved,
    layer_check_tables,
    resolve_check_commands,
    resolve_check_states,
    story_settings_for,
)
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
from lithos_loom.plugins.story_develop.profiles import (
    UnknownProfileError,
    get_profile,
    resolve_profile,
)
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
    "pr_closed": 2,
    # the project's current config could not be resolved — gated with
    # nothing rather than with built-ins (PRD S3: skipped loudly)
    "config_unresolved": 4,
}


def merge_gate_command(
    change: str = typer.Argument(
        ...,
        help="The PR to trial-merge: #142 / 142 / a GitHub PR URL.",
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        "-p",
        help="Review Profile whose check-set gates the merge result (default: "
        "the story's, else the host's default_review_profile, else standard).",
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
    image: str | None = typer.Option(
        None,
        "--image",
        help="Sandbox image the checks run in (default: the story's "
        "develop_image, else the built-in).",
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
    story: str | None = typer.Option(
        None,
        "--story",
        help=(
            "Lithos task id of the story behind this PR: resolve its project's "
            "and its own develop_* settings (profile, check-set, image, test "
            "command, parity) exactly as the daemon path does — the CURRENT "
            "config defending that project; explicit flags still win."
        ),
    ),
    config: Path | None = typer.Option(None, "--config", help="Host config path."),
) -> None:
    """Trial-merge a PR into its current base and run the check-set on the result."""
    if profile is not None:
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
        # `is not None`, not truthiness: a blank --image must fail closed like
        # every other blank value, never fall through to the story / default
        explicit_image = (
            parse_image(image, where="--image") if image is not None else None
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    repo = repo or Path.cwd()
    host = load_config(config)

    # The project's CURRENT config is what defends its base (PRD S3): a
    # story's resolved develop_* settings are the base layer, explicit flags
    # win, and without a story the host's default profile applies.
    story_layer: dict = {}
    if story is not None:
        try:
            story_layer, _settings = story_settings_for(host, story, strict=True)
        except StorySettingsUnresolved as exc:
            typer.echo(f"merge-gate {change}: config_unresolved")
            for reason in exc.reasons:
                typer.echo(f"  {reason}")
            typer.echo(
                "  the project's current config could not be resolved; nothing "
                "was gated (S3 gates with the current config or not at all)"
            )
            raise typer.Exit(EXIT_CODES["config_unresolved"]) from exc
    section = getattr(host, "story_develop", None)
    if profile is not None or story_layer.get("review_profile"):
        effective_profile = profile or story_layer["review_profile"]
    else:
        # the host default goes through the same unknown-profile policy the
        # daemon applies (halt, or the strongest configured profile)
        policy = resolve_profile(
            task_value=None,
            project_value=None,
            host_value=getattr(section, "default_review_profile", None)
            if section
            else None,
            unknown_profile=getattr(section, "unknown_profile", "halt")
            if section
            else "halt",
        )
        for friction in policy.frictions:
            typer.secho(f"[Friction] {friction}", err=True, fg=typer.colors.YELLOW)
        if policy.halt:
            typer.secho(
                "error: the host default_review_profile is not defined "
                "(unknown_profile=halt); not running",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(2)
        effective_profile = policy.profile.name
    resolved_image = explicit_image or story_layer.get("image") or DEFAULT_IMAGE

    # Forks are answered from GitHub's metadata before any fetch: the sweep
    # must never pull a third-party head into the operator's checkout.
    resolved = resolve_change(repo, change, base_branch="main", allow_fork=False)
    if not resolved.head_branch:
        raise typer.BadParameter(
            f"merge-gate takes a PR (it trial-merges the PR's base and may push "
            f"the update to the PR branch); {change!r} resolved to a range / "
            "branch with no PR."
        )

    explicit: dict = {
        k: v
        for k, v in {
            "test_command": test_command,
            "parity_command": parity_command,
        }.items()
        if v is not None
    }
    gate_keys = ("test_gate", "test_command", "parity_command")
    develop_config = DevelopConfig(
        **{
            **dict(
                repo=repo,
                description=f"merge-gate {resolved.head_ref}",
                work_dir=host.orchestrator.work_dir / "merge-gate",
                test_timeout=test_timeout,
            ),
            **{k: story_layer[k] for k in gate_keys if k in story_layer},
            **explicit,
            # the check tables merge per key, explicit flags over the story's
            **layer_check_tables(
                story_layer, check_commands=check_commands, check_states=check_states
            ),
            "review_profile": effective_profile,
            "image": resolved_image,
        }
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
