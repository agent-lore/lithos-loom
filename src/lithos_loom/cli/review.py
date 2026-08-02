"""``lithos-loom develop review`` — run the panel + gate on an existing change.

Review-only mode (#154): resolve a PR number / ref range / local branch to a
``base..head``, run the resolved profile's reviewer panel + deterministic gate
against the head tree once (no coder, no fix loop), and emit a local report
(human markdown to stdout, structured JSON via ``--json``). Exits non-zero when
the review is blocking.

The command is a thin wrapper over :func:`review_only.review_change` — the same
function the review-correctness eval harness (#183) drives.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from lithos_loom.config import load_config
from lithos_loom.plugins.story_develop.config import (
    DEFAULT_TEST_TIMEOUT,
    DevelopConfig,
    ReviewerSpec,
    parse_check_commands,
)
from lithos_loom.plugins.story_develop.daemon_io import profile_panel
from lithos_loom.plugins.story_develop.personas import canonical_personas
from lithos_loom.plugins.story_develop.profiles import (
    UnknownProfileError,
    get_profile,
)
from lithos_loom.plugins.story_develop.review_only import review_change
from lithos_loom.plugins.story_develop.review_resolve import resolve_change


def review_command(
    change: str = typer.Argument(
        ...,
        help="What to review: a PR (#142 / 142 / PR URL), a ref range (a..b), "
        "or a local branch / ref.",
    ),
    profile: str = typer.Option(
        "standard",
        "--profile",
        "-p",
        help="Review profile (selects panel + check-set).",
    ),
    reviewer: list[str] | None = typer.Option(
        None, "--reviewer", help="Override the panel personas (repeatable)."
    ),
    acceptance: str | None = typer.Option(
        None, "--ac", help="Acceptance criteria text (the change's intent)."
    ),
    acceptance_file: Path | None = typer.Option(
        None, "--ac-file", help="Read acceptance criteria from a file."
    ),
    base: str | None = typer.Option(
        None, "--base", help="Override the base ref (default: merge-base with main)."
    ),
    check_command: list[str] | None = typer.Option(
        None,
        "--check-command",
        help="Override a gate check's command as NAME=COMMAND (repeatable), e.g. "
        "--check-command typecheck='make typecheck'. Runs the repo's own command "
        "verbatim instead of the catalog default (which can over-scope). "
        "Overridable: lint / typecheck / sast / dep-audit / coverage / semgrep "
        "(the `test` check uses --test-command).",
    ),
    test_timeout: int = typer.Option(
        DEFAULT_TEST_TIMEOUT,
        "--test-timeout",
        help="Max seconds for one gate check run (the test check, other check-set "
        "checks, and autoformat). Raise it for a repo whose suite exceeds the "
        "default — otherwise the gate floor times out and blocks the report.",
    ),
    repo: Path | None = typer.Option(
        None, "--repo", help="Repository to review in (default: current directory)."
    ),
    json_out: Path | None = typer.Option(
        None, "--json", help="Write the structured JSON report to this path."
    ),
    keep_worktree: bool = typer.Option(
        False, "--keep-worktree", help="Keep the review worktree for inspection."
    ),
    config: Path | None = typer.Option(None, "--config", help="Host config path."),
) -> None:
    """Run the reviewer panel + deterministic gate against an existing change."""
    # Fail closed on an unknown profile rather than silently running `standard`
    # while mislabeling the report — validate the explicit name through the single
    # known-profile seam (get_profile, the same funnel resolve_profile / the eval
    # case loader use), so the known set lives in exactly one place.
    try:
        get_profile(profile)
    except UnknownProfileError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if test_timeout < 1:
        raise typer.BadParameter("--test-timeout must be at least 1 second")
    check_commands = resolve_check_commands(check_command)
    repo = repo or Path.cwd()
    host = load_config(config)

    resolved = resolve_change(repo, change, base_branch="main", base_override=base)

    criteria = resolve_acceptance_criteria(acceptance, acceptance_file, resolved.body)
    if not criteria:
        typer.secho(
            "error: no acceptance criteria for the review — pass --ac / --ac-file "
            "(a PR's body is used automatically, but this change has none).",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    reviewers = resolve_reviewers(profile, reviewer)

    develop_config = DevelopConfig(
        repo=repo,
        description=resolved.title or f"Review of {resolved.head_ref}",
        work_dir=host.orchestrator.work_dir / "review",
        acceptance_criteria=criteria,
        review_profile=profile,
        reviewers=reviewers,
        base_branch=base or "main",
        test_timeout=test_timeout,
        check_commands=check_commands,
    )

    report = review_change(develop_config, resolved, keep_worktree=keep_worktree)

    typer.echo(report.to_markdown())
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(report.to_json(), indent=2), encoding="utf-8")

    raise typer.Exit(1 if report.blocking else 0)


def resolve_acceptance_criteria(
    acceptance: str | None, acceptance_file: Path | None, pr_body: str
) -> str:
    """Acceptance criteria precedence: ``--ac-file`` > ``--ac`` > PR body."""
    if acceptance_file is not None:
        return acceptance_file.read_text(encoding="utf-8").strip()
    if acceptance:
        return acceptance.strip()
    return pr_body.strip()


def resolve_reviewers(
    profile: str, reviewer: list[str] | None
) -> tuple[ReviewerSpec, ...]:
    """Explicit ``--reviewer`` names win; otherwise the profile's persona panel.

    A ``--reviewer NAME`` resolves to its **canonical persona** (#137 — engine,
    block threshold, effort, focus prompt baked in), matching the daemon's
    resolver; an unknown name fails closed. With no override, the profile's
    persona panel drives the run (empty tuple for a gate-only profile, so
    ``DevelopConfig`` uses its built-in reviewer).
    """
    if reviewer:
        registry = canonical_personas()
        specs: list[ReviewerSpec] = []
        for name in reviewer:
            spec = registry.get(name)
            if spec is None:
                raise typer.BadParameter(
                    f"unknown reviewer persona {name!r}; "
                    f"known: {', '.join(sorted(registry))}"
                )
            if spec not in specs:
                specs.append(spec)
        return tuple(specs)
    panel = profile_panel(profile, [])
    return panel if panel is not None else ()


def resolve_check_commands(check_command: list[str] | None) -> dict[str, str]:
    """Parse repeatable ``--check-command NAME=COMMAND`` into a ``{check: command}``
    map (#273). Shared by ``review`` and ``converge``.

    Each item is split on the first ``=`` (so a command may itself contain ``=``);
    a missing ``=`` fails closed. The assembled map is validated through
    :func:`config.parse_check_commands` so the CLI and project-metadata surfaces
    reject the same garbage (unknown / non-overridable check, empty command)
    identically. A malformed value raises :class:`typer.BadParameter` (exit 2) before
    any container work.
    """
    if not check_command:
        return {}
    raw: dict[str, str] = {}
    for item in check_command:
        name, sep, command = item.partition("=")
        if not sep:
            raise typer.BadParameter(
                f"--check-command must be NAME=COMMAND (got {item!r})"
            )
        raw[name.strip()] = command
    try:
        return parse_check_commands(raw, where="--check-command")
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
