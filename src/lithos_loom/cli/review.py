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
from dataclasses import replace
from pathlib import Path

import typer

from lithos_loom.config import load_config
from lithos_loom.plugins.story_develop.config import (
    DEFAULT_TEST_TIMEOUT,
    DevelopConfig,
    ReviewerSpec,
    parse_check_command_pairs,
    parse_check_state_pairs,
    parse_parity_command,
    parse_test_command,
)
from lithos_loom.plugins.story_develop.daemon_io import (
    load_tool_default_models,
    profile_panel,
)
from lithos_loom.plugins.story_develop.model_policy import (
    apply_panel_default_models,
    require_agent_models,
)
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
    check_state: list[str] | None = typer.Option(
        None,
        "--check-state",
        help="Override a gate check's blocking state as NAME=STATE (repeatable): "
        "required | informational | off, e.g. --check-state sast=off. `off` drops "
        "the check cleanly. Stateable: lint / typecheck / test / sast / dep-audit / "
        "coverage / semgrep.",
    ),
    test_command: str | None = typer.Option(
        None,
        "--test-command",
        help="Command for the `test` gate check (overrides auto-detection). The `test` "
        "check has bespoke detection, so it takes this dedicated flag rather than "
        "--check-command.",
    ),
    parity_command: str | None = typer.Option(
        None,
        "--parity-command",
        help="The repo's aggregate verification command (e.g. 'make check'), run once "
        "as a required `repo-parity` gate check so the reviewed tree passes what CI "
        "enforces beyond the structured check-set. Primary gate for ecosystems the "
        "catalog doesn't model (C/C++).",
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
    check_states = resolve_check_states(check_state)
    # Validate --test-command through the shared normaliser: a blank / whitespace-only
    # value would otherwise reach `sh -c` unmodified, do no work, exit 0, and
    # false-green the required `test` check without running tests (#278 review).
    try:
        test_command = parse_test_command(test_command, where="--test-command")
        parity_command = parse_parity_command(parity_command, where="--parity-command")
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
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
        test_command=test_command,
        test_timeout=test_timeout,
        check_commands=check_commands,
        check_states=check_states,
        parity_command=parity_command,
    )
    develop_config = apply_model_policy(develop_config, where="develop review")

    report = review_change(develop_config, resolved, keep_worktree=keep_worktree)

    typer.echo(report.to_markdown())
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(report.to_json(), indent=2), encoding="utf-8")

    raise typer.Exit(1 if report.blocking else 0)


def apply_model_policy(
    config: DevelopConfig, *, where: str, include_coder: bool = False
) -> DevelopConfig:
    """Make every agent's model explicit, or fail closed (#304).

    Applies the loom TOML's per-tool default models to the run's EFFECTIVE
    panel — ``effective_reviewers``, so the built-in fallback reviewer a
    gate-only profile folds in is covered too — then rejects anything still
    on ``model=None`` before a container starts. *include_coder* extends the
    policy to ``coder_model`` on surfaces that run one (``develop converge``);
    ``develop review`` is review-only. Shared by both commands.
    """
    default_models, frictions = load_tool_default_models()
    for friction in frictions:
        typer.echo(f"[Friction] {friction}", err=True)
    panel = apply_panel_default_models(config.effective_reviewers, default_models)
    coder_model = config.coder_model
    if include_coder and coder_model is None:
        coder_model = default_models.get(config.coder)
    try:
        require_agent_models(
            panel=panel,
            coder=config.coder if include_coder else None,
            coder_model=coder_model,
            where=where,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return replace(config, reviewers=panel, coder_model=coder_model)


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

    Delegates to the framework-neutral :func:`config.parse_check_command_pairs` (also
    used by the ``__main__`` route flag), surfacing any :class:`ValueError` as a
    :class:`typer.BadParameter` (exit 2) before any container work — so the CLI and the
    project-metadata surface reject the same garbage identically.
    """
    try:
        return parse_check_command_pairs(check_command, where="--check-command")
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def resolve_check_states(check_state: list[str] | None) -> dict[str, str]:
    """Parse repeatable ``--check-state NAME=STATE`` into a ``{check: state}`` map
    (#273 slice 2). Shared by ``review`` and ``converge``.

    Delegates to :func:`config.parse_check_state_pairs` (also used by the ``__main__``
    route flag), surfacing any :class:`ValueError` as a :class:`typer.BadParameter`
    (exit 2) — so the CLI and the project-metadata surface reject the same garbage.
    """
    try:
        return parse_check_state_pairs(check_state, where="--check-state")
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
