"""``lithos-loom develop brief-review`` (604fb936): the brief review, on demand.

Runs the pass dispatch will run before a coder starts — one read-only agent
turn checking the story's brief against the tree the coder would start from —
and prints the addendum it would hold the story for. It writes **nothing** to
Lithos: no gate, no record, no description change. It is how an operator
tries the pass on a project before switching it on, and how the pass was
compared against the lens T3 pilot's hand-written addenda (``--brief-file``
re-runs a brief without the addendum already appended to it).

What it resolves is what dispatch resolves: the story's project checkout
(``[projects.<slug>].repo``), its develop settings (the coder's engine,
model and effort run the review), and the base — ``origin/main`` fetched now,
the sha a coder's worktree would be cut at — unless ``--base`` names one.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import typer

from lithos_loom.cli.review import (
    apply_model_policy,
    host_default_models,
    story_settings_for,
)
from lithos_loom.config import load_config
from lithos_loom.plugins.story_develop.brief_review import (
    MODE_DELTA,
    MODE_FULL,
    BriefInputs,
    render_addendum,
    review_brief,
)
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.daemon_io import fetch_task
from lithos_loom.plugins.story_develop.lithos_io import explicit_acceptance_criteria
from lithos_loom.runner import worktree

# The base branch a dispatched coder is cut from (the story-develop route
# passes no --branch, so `main`).
_BASE_BRANCH = "main"


def brief_review_command(
    story: str = typer.Argument(
        ..., help="The story whose brief to review: a Lithos task id or prefix."
    ),
    base: str | None = typer.Option(
        None,
        "--base",
        help="The commit to review against (default: origin/main fetched now — "
        "the sha a dispatched coder's worktree would be cut at).",
    ),
    delta_from: str | None = typer.Option(
        None,
        "--delta-from",
        help="Recheck instead: the base an approved review stood on. The "
        "reviewer reports only what changed between it and --base.",
    ),
    brief_file: Path | None = typer.Option(
        None,
        "--brief-file",
        help="Review this text instead of the story's description (e.g. a brief "
        "without the addendum already appended to it).",
    ),
    repo: Path | None = typer.Option(
        None,
        "--repo",
        help="The checkout to review in (default: the story's project's "
        "[projects.<slug>].repo).",
    ),
    timeout: int = typer.Option(
        1800, "--timeout", help="Max seconds for one review turn."
    ),
    config: Path | None = typer.Option(None, "--config", help="Host config path."),
) -> None:
    """Draft a story's brief-review addendum and print it (writes nothing)."""
    host = load_config(config)
    url = host.orchestrator.lithos_url
    try:
        task = fetch_task(url, story)
    except Exception as exc:  # noqa: BLE001 — reported, never a traceback
        _refuse(f"story {story}: {exc}")
    metadata = dict(task.metadata) if isinstance(task.metadata, Mapping) else {}
    checkout = repo or _project_repo(host, metadata.get("project"))

    base_sha = (
        _resolve(checkout, base, "--base")
        if base
        else worktree.current_base_ref(checkout, _BASE_BRANCH)
    )
    prior_base = _resolve(checkout, delta_from, "--delta-from") if delta_from else None
    brief = (
        brief_file.read_text(encoding="utf-8")
        if brief_file is not None
        else task.description or ""
    )

    overrides, _settings = story_settings_for(host, task.id)
    develop_config = apply_model_policy(
        DevelopConfig(
            **{
                "repo": checkout,
                "description": task.title,
                "work_dir": host.orchestrator.work_dir / "brief-review",
                **overrides,
            }
        ),
        where="develop brief-review",
        default_models=host_default_models(host),
        include_coder=True,
    )
    mode = MODE_DELTA if prior_base else MODE_FULL
    result = review_brief(
        develop_config,
        BriefInputs(
            story_id=task.id,
            title=task.title,
            brief=brief,
            prd=_text(metadata.get("prd")),
            prd_sections=_text(metadata.get("prd_sections")),
            written_at=task.created_at.isoformat() if task.created_at else None,
            acceptance_criteria=explicit_acceptance_criteria(metadata),
        ),
        base_sha=base_sha,
        mode=mode,
        prior_base=prior_base,
        timeout=timeout,
    )

    if result.addendum is None:
        typer.secho(
            f"brief review degraded: {result.note} (${result.cost_usd:.2f})",
            err=True,
            fg=typer.colors.RED,
        )
        if result.raw:
            typer.echo(result.raw)
        raise typer.Exit(1)
    addendum = result.addendum
    typer.echo(
        render_addendum(
            addendum,
            base_sha=base_sha,
            on=datetime.now(UTC).date(),
            mode=mode,
            prior_base=prior_base,
        ),
        nl=False,
    )
    counts = (
        "no change"
        if addendum.no_change is not None and not addendum.items
        else f"{_n(len(addendum.scope_cuts), 'scope cut')}, "
        f"{_n(len(addendum.facts), 'fact')}, "
        f"{_n(len(addendum.decisions), 'decision')}"
    )
    typer.secho(
        f"brief review ({mode}) at {base_sha[:12]}: {counts} — "
        f"${result.cost_usd:.2f}. Nothing was written to Lithos.",
        err=True,
        fg=typer.colors.GREEN,
    )


def _project_repo(host: object, slug: object) -> Path:
    projects = getattr(host, "projects", {}) or {}
    if not isinstance(slug, str) or not slug:
        _refuse(
            "the story names no project (`metadata.project`); pass --repo with "
            "the checkout to review in"
        )
    project = projects.get(slug)
    if project is None:
        _refuse(
            f"project {slug!r} is not mapped in this host's config — add a "
            f"[projects.{slug}] stanza with its `repo` path, or pass --repo"
        )
    return Path(project.repo)


def _resolve(checkout: Path, ref: str, flag: str) -> str:
    """*ref* as a full commit sha in *checkout*, or a refusal naming it."""
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        _refuse(f"{flag} {ref!r} is not a commit in {checkout}")
    return sha


def _refuse(message: str) -> NoReturn:
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    raise typer.Exit(2)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"
