"""``lithos-loom develop resume`` — continue a run that died mid-loop.

The on-demand half of 5dbeb0c8 slice C. A story-develop run whose HOST died
mid-loop — a revoked OAuth token, a coder container that vanished — has every
round it paid for committed on a local branch, and its per-round checkpoint
(:mod:`~lithos_loom.plugins.story_develop.checkpoint`) says which branch, at
which commit, off which base, after how many rounds and how much spend. The
daemon resumes such a run by itself when the operator ticks its needs-human
gate; this is the same entry for a run nobody is going to re-dispatch: a
standalone ``python -m lithos_loom.plugins.story_develop`` run, a run whose
route no longer matches, or one the operator simply wants to continue by hand.

It runs the loop and nothing else: a fresh worktree on a new branch at the dead
run's head, the last review round's findings as the coder's intake, the
REMAINDER of the branch's round and cost budgets, and the story's current
``develop_*`` settings. No PR, no Lithos write, no gate — an approved resume
leaves a branch, and ``develop deliver <run>`` is what turns a branch into a
monitored PR.

Refusals are the shared ones (:func:`~…story_develop.resume.prepare_resume`):
a run with no checkpointed committed round, a head the repo no longer has, or a
branch whose rounds / spend already meet the ceiling. All are "continuing this
run buys nothing" — none of them is repaired by trying again, so each exits 2
with the sentence saying which.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import typer

from lithos_loom.cli.review import (
    apply_model_policy,
    host_default_models,
    resolve_reviewers,
    story_settings_for,
)
from lithos_loom.config import load_config
from lithos_loom.plugins.story_develop import run_outcome
from lithos_loom.plugins.story_develop.checkpoint import round_checkpoint
from lithos_loom.plugins.story_develop.config import (
    DEFAULT_IMAGE,
    DevelopConfig,
    parse_image,
)
from lithos_loom.plugins.story_develop.daemon_io import read_task_payload
from lithos_loom.plugins.story_develop.develop import develop
from lithos_loom.plugins.story_develop.profiles import UnknownProfileError, get_profile
from lithos_loom.plugins.story_develop.resume import (
    prepare_resume,
    record_resumed_from,
)


def _fail(message: str) -> None:
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    raise typer.Exit(2)


def _task_text(run_dir: Path) -> tuple[str, str | None, str]:
    """The task the dead run was developing: ``(description, criteria, task_id)``.

    From the run dir's own ``task.json`` snapshot — the envelope the plugin
    copied in at run start, which is THIS run's task even after the shared
    per-task file was overwritten by a later dispatch. A run without one (an
    older run, or a standalone one) has no task text on disk and the operator
    supplies it with ``--description``.
    """
    try:
        ctx = read_task_payload(run_dir / "task.json")
    except ValueError:
        return "", None, ""
    return ctx.task_text, ctx.acceptance_criteria, ctx.task_id


def resume_command(
    run: str = typer.Argument(
        ...,
        help="The run to continue: a run id, or a task id (its newest run).",
    ),
    repo: Path | None = typer.Option(
        None,
        "--repo",
        help="Repository to work in (default: the repo the dead run recorded "
        "in its checkpoint).",
    ),
    story: str | None = typer.Option(
        None,
        "--story",
        help="Lithos task id whose project / task develop_* settings this run "
        "uses — rounds, profile, panel, check-set, image (default: the run "
        "dir's own task id; pass --no-story-settings to skip the lookup).",
    ),
    no_story_settings: bool = typer.Option(
        False,
        "--no-story-settings",
        help="Do not read settings from Lithos at all; use the host defaults "
        "plus the flags given here (for a host with no Lithos reachable).",
    ),
    description: str | None = typer.Option(
        None,
        "--description",
        help="The task text, for a run dir with no task.json snapshot.",
    ),
    profile: str | None = typer.Option(
        None, "--profile", "-p", help="Review profile (default: the story's own)."
    ),
    max_rounds: int | None = typer.Option(
        None,
        "--max-rounds",
        help="Round ceiling for the WHOLE branch, not for this run: the resumed "
        "run gets what the rounds already landed leave of it.",
    ),
    max_cost: float | None = typer.Option(
        None,
        "--max-cost",
        help="USD ceiling for the WHOLE branch, not for this run: the resumed "
        "run gets what the dead run's spend leaves of it.",
    ),
    image: str | None = typer.Option(
        None, "--image", help="Sandbox container image (default: the story's)."
    ),
    base: str | None = typer.Option(
        None,
        "--base",
        help="Base branch the branch lands on (default: main). Only the review "
        "range consults it — the worktree is created at the dead run's head, "
        "and the checkpoint's own recorded base ref wins when it has one.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Resolve everything and print what would be continued — the "
        "branch, the head, the rounds and budget left, the intake round — "
        "without starting a container.",
    ),
    config: Path | None = typer.Option(None, "--config", help="Host config path."),
) -> None:
    """Continue a run that died mid-loop, on its own branch."""
    if profile is not None:
        try:
            get_profile(profile)
        except UnknownProfileError as exc:
            raise typer.BadParameter(str(exc)) from exc
    if max_rounds is not None and max_rounds < 1:
        raise typer.BadParameter("--max-rounds must be at least 1")
    # NaN compares False against everything, so `<= 0` alone would let
    # `--max-cost nan` through as an effectively-unlimited budget.
    if max_cost is not None and (not math.isfinite(max_cost) or max_cost <= 0):
        raise typer.BadParameter("--max-cost must be a finite value greater than 0")
    try:
        resolved_image = parse_image(image, where="--image") if image else None
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    host = load_config(config)
    run_dir = run_outcome.resolve_run_dir(host.orchestrator.work_dir, run)
    if run_dir is None:
        _fail(f"no run dir for {run!r} under {host.orchestrator.work_dir}")
        return
    if run_outcome.is_converge_run_dir(run_dir):
        _fail(
            f"{run_dir.name} is a `develop converge` run, not a story-develop "
            "one: its rounds belong on the PR it was converging — see "
            "`lithos-loom develop converge-push`"
        )
        return
    checkpoint = round_checkpoint(run_dir)
    if checkpoint is None:
        _fail(
            f"{run_dir.name} recorded no round boundary; there is nothing to "
            "continue (it died before its first round finished, or it predates "
            "per-round checkpointing)"
        )
        return

    task_text, criteria, snapshot_task = _task_text(run_dir)
    task_text = description or task_text
    if not task_text:
        _fail(
            f"{run_dir.name} has no task.json snapshot to take the task text "
            "from; pass --description"
        )
        return
    repo_path = repo or (Path(checkpoint.repo) if checkpoint.repo else None)
    if repo_path is None:
        _fail(f"{run_dir.name}'s checkpoint records no repo; pass --repo")
        return

    story_layer: dict[str, Any] = {}
    story_id = story or snapshot_task
    if not no_story_settings and story_id:
        story_layer, _settings = story_settings_for(host, story_id)
    explicit: dict[str, Any] = {
        key: value
        for key, value in {"max_rounds": max_rounds, "max_cost_usd": max_cost}.items()
        if value is not None
    }
    if profile is not None:
        explicit["review_profile"] = profile
        explicit["reviewers"] = resolve_reviewers(profile, None)
    config_for_run = DevelopConfig(
        **{
            **dict(
                repo=repo_path,
                description=task_text,
                # the same per-task dir, so the resumed run lists and delivers
                # beside the run it continues
                work_dir=run_dir.parent,
                acceptance_criteria=criteria,
                base_branch=base or "main",
            ),
            **story_layer,
            **explicit,
            "image": resolved_image or story_layer.get("image") or DEFAULT_IMAGE,
        }
    )
    config_for_run = apply_model_policy(
        config_for_run,
        where="develop resume",
        default_models=host_default_models(host),
        include_coder=True,
    )

    resumption, refused = prepare_resume(config_for_run, run_dir)
    if resumption is None:
        _fail(f"not resuming — {refused}")
        return
    typer.echo(resumption.note)
    if dry_run:
        typer.echo("--dry-run: nothing started")
        raise typer.Exit(0)

    record_resumed_from(resumption.config.run_dir, resumption.plan)
    result = develop(resumption.config, entry=resumption.entry)
    typer.echo(f"run {result.run_id}: {result.status.upper()} — {result.message}")
    if not result.approved:
        raise typer.Exit(1)
    typer.echo(
        f"the branch is local only — `lithos-loom develop deliver {result.run_id}` "
        "pushes it, opens its PR and raises the pr gate"
    )
