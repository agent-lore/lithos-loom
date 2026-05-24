"""``lithos-loom project`` sub-app (Slice 3 capture-macro helper, Slice 4 D23/D30).

Slice 4 reframes the source of truth: per **D23**, **Lithos is the
canonical project registry** (slug, status, tags, context body) and
the TOML ``[projects.<slug>]`` table is just a host-local automation
overlay (working-tree path, tool-config overrides). The intersection
is the slug.

Per **D30** the default ``project list`` shape is therefore:

    slug              status    local
    lithos-loom       active    ✓ (/home/dns/projects/lithos/code/lithos-loom)
    influx            active    ✓ (/home/dns/projects/lithos/code/influx)
    edgelands         active    ✗ (no TOML entry on this host)
    old-experiment    archived  ✓ (/home/dns/projects/old-experiment)

— enumerating Lithos via ``lithos_list(path_prefix="projects/",
tags=["project-context"])`` and marking each row with whether the
local TOML has an automation entry for that slug.

``--source toml`` preserves the pre-Slice-4 path for hosts that
don't have a Lithos connection available (Track 2 plugin runners,
disconnected workstations). The capture macro's
``--format json`` invocation gets a stable contract on both sources:
a JSON array of slug strings, in alphabetical order.

When Slice 5 lands the create-project macro + ``project import`` CLI,
they live in this same sub-app.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import typer

from lithos_loom.config import LoomConfig, load_config
from lithos_loom.errors import LithosClientError, LithosLoomError
from lithos_loom.lithos_client import LithosClient, NoteSummary

project_app = typer.Typer(
    name="project",
    help="Project-config-aware CLI helpers (Slice 3+).",
    no_args_is_help=True,
)


# Output formats; explicit enum strings give Typer a stable
# completion list and prevent typos from silently falling through to
# the plain-text default.
_FORMAT_TEXT = "text"
_FORMAT_JSON = "json"

# Source modes. ``lithos`` (default) enumerates from the canonical KB
# registry per D23. ``toml`` falls back to the local TOML
# ``[projects]`` table — for hosts without a Lithos connection or
# when the operator wants to inspect their host-local overlay in
# isolation.
_SOURCE_LITHOS = "lithos"
_SOURCE_TOML = "toml"

_PROJECTS_PATH_PREFIX = "projects/"
_PROJECT_CONTEXT_TAG = "project-context"


@dataclass(frozen=True)
class _ProjectRow:
    """One row of ``project list`` output.

    Carries the union of Lithos-side and TOML-side data so the
    formatters (text / json) can decide what to render without
    re-merging.
    """

    slug: str
    status: str | None  # Lithos status; None for TOML-only rows or unknown
    local: bool  # has a TOML entry on this host
    repo: str | None  # local working-tree path; None when no TOML entry


@project_app.command("list")
def project_list(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path (overrides LITHOS_LOOM_CONFIG).",
    ),
    output_format: str = typer.Option(
        _FORMAT_TEXT,
        "--format",
        "-f",
        help="Output format: 'text' (aligned columns) or 'json' "
        "(array of slugs — stable shape for the capture macro).",
    ),
    source: str = typer.Option(
        _SOURCE_LITHOS,
        "--source",
        "-s",
        help=(
            "Where to enumerate from: 'lithos' (default, D23 canonical) "
            "queries Lithos's projects/ KB. 'toml' falls back to the local "
            "[projects] table — useful when Lithos is unreachable or you "
            "want to inspect host-local overlay only."
        ),
    ),
) -> None:
    """List projects with their Lithos-canonical status + TOML-local overlay.

    Default (``--source lithos``) queries
    ``lithos_list(path_prefix="projects/", tags=["project-context"])``
    and joins the result against the local TOML ``[projects]`` table
    to mark which slugs have automation configured on this host. Slugs
    present only in TOML (no Lithos doc) are NOT listed here — they're
    surfaced by ``lithos-loom doctor`` instead, which calls them out
    as misconfigured (a TOML entry referencing a slug Lithos doesn't
    know about).

    ``--source toml`` enumerates TOML slugs only — same shape as the
    pre-Slice-4 command, useful for offline hosts.
    """
    try:
        cfg = load_config(config)
    except LithosLoomError as exc:
        typer.echo(f"lithos-loom: {exc}", err=True)
        sys.exit(1)

    if source == _SOURCE_TOML:
        rows = _rows_from_toml(cfg)
    elif source == _SOURCE_LITHOS:
        try:
            rows = asyncio.run(_rows_from_lithos(cfg))
        except OSError as exc:
            typer.echo(
                f"lithos-loom: could not reach Lithos at "
                f"{cfg.orchestrator.lithos_url} ({exc}); try --source toml "
                f"to fall back to the local [projects] table",
                err=True,
            )
            sys.exit(1)
        except LithosClientError as exc:
            typer.echo(f"lithos-loom: lithos_list failed: {exc}", err=True)
            sys.exit(1)
    else:
        typer.echo(
            f"lithos-loom: unknown --source {source!r} "
            f"(expected one of: {_SOURCE_LITHOS}, {_SOURCE_TOML})",
            err=True,
        )
        sys.exit(2)

    if output_format == _FORMAT_JSON:
        # Stable shape across both sources: a JSON array of slug
        # strings. The capture macro's existing
        # ``JSON.parse(... project list --format json)`` consumer
        # works unchanged.
        typer.echo(json.dumps([row.slug for row in rows]))
        return
    if output_format == _FORMAT_TEXT:
        _print_text_rows(rows)
        return
    typer.echo(
        f"lithos-loom: unknown --format {output_format!r} "
        f"(expected one of: {_FORMAT_TEXT}, {_FORMAT_JSON})",
        err=True,
    )
    sys.exit(2)


def _rows_from_toml(cfg: LoomConfig) -> list[_ProjectRow]:
    """Pre-Slice-4 enumeration path. Slugs from ``cfg.projects.keys()``,
    alphabetised, no Lithos round-trip. ``status`` is ``None`` because
    we don't know it without asking Lithos."""
    return [
        _ProjectRow(
            slug=slug,
            status=None,
            local=True,
            repo=str(cfg.projects[slug].repo),
        )
        for slug in sorted(cfg.projects)
    ]


async def _rows_from_lithos(cfg: LoomConfig) -> list[_ProjectRow]:
    """Default Slice 4 path. Enumerates Lithos via
    ``note_list(path_prefix="projects/", tags=["project-context"])``
    and joins against ``cfg.projects`` to mark local-overlay rows.

    The async wrapper exists because :class:`LithosClient` is an
    async context manager — Typer's command is sync, so we wrap with
    ``asyncio.run`` at the call site (same pattern as ``task create``).
    """
    async with LithosClient(
        cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
    ) as client:
        summaries = await client.note_list(
            path_prefix=_PROJECTS_PATH_PREFIX,
            tags=[_PROJECT_CONTEXT_TAG],
        )
    return _merge_lithos_with_toml(summaries, cfg.projects)


def _merge_lithos_with_toml(
    summaries: list[NoteSummary],
    toml_projects: Mapping[str, object],
) -> list[_ProjectRow]:
    """Join Lithos's per-doc summaries with the host-local TOML map.

    Lithos-side slugs are derived from the doc path's first segment
    after ``projects/`` (see :func:`lithos_client._slug_from_path`).
    Empty slugs (path didn't match the expected shape) are dropped
    — there's nothing for the operator to act on.

    Multiple Lithos docs may share a slug (a project with both a
    ``<slug>-project-context.md`` and an ``architecture.md`` under
    the same slug directory). We collapse on the slug — one row per
    slug. The status column reflects the **canonical project context
    doc** (``projects/<slug>/<slug>-project-context.md``) when one
    exists for the slug. That's the doc the project-context registry
    actually means by "the project's status"; other docs
    (architecture, roadmap, etc.) live alongside but aren't the
    registry entry, so their status flips wouldn't reflect what the
    operator means by "is this project active".

    The ``<slug>-project-context.md`` naming convention matches what
    real prod project-context docs use today (e.g.
    ``projects/lithos-loom/lithos-loom-project-context.md``).
    Earlier the picker looked for literal ``context.md`` — a clean
    name in isolation, but it never matched prod docs, so the
    canonical preference silently became dead code in practice. See
    the soak-phase note in ``examples/slice-4-test/MANUAL_TEST.md``.

    When no ``<slug>-project-context.md`` is present for the slug
    (operator structured the project differently, or this is a
    test fixture), we fall back to the summary with the
    lexicographically-smallest path so the choice is deterministic
    regardless of Lithos's response order. Without this rule the
    displayed status was list-order dependent; could flip between
    ``active`` and ``archived`` on the same operator state if Lithos
    returned summaries in a different order.

    Per-doc visibility lives in a separate command (``project docs
    <slug>`` — future).
    """
    # Group all summaries by slug first; we need to inspect all the
    # candidates for a slug before picking the canonical one rather
    # than committing to the first-seen.
    by_slug: dict[str, list[NoteSummary]] = {}
    for summary in summaries:
        slug = summary.slug
        if not slug:
            continue
        by_slug.setdefault(slug, []).append(summary)

    rows: list[_ProjectRow] = []
    for slug in sorted(by_slug):
        canonical = _pick_canonical_summary(slug, by_slug[slug])
        is_local = slug in toml_projects
        repo = str(getattr(toml_projects[slug], "repo", "")) if is_local else None
        rows.append(
            _ProjectRow(
                slug=slug,
                status=canonical.status,
                local=is_local,
                repo=repo,
            )
        )
    return rows


def _pick_canonical_summary(slug: str, candidates: list[NoteSummary]) -> NoteSummary:
    """Pick the project-context doc whose status represents the slug.

    Preference order:

    1. ``projects/<slug>/<slug>-project-context.md`` — the prod
       convention for canonical project context registry entries
       (e.g. ``projects/lithos-loom/lithos-loom-project-context.md``).
       Other doctypes alongside it (``architecture.md``,
       ``roadmap.md``, ad-hoc notes) are supplementary; their status
       flips don't represent "is the project active".
    2. Lexicographically-smallest path among the remaining
       candidates. Deterministic regardless of Lithos's response
       order — without this fallback, two docs both labelled
       supplementary (no ``<slug>-project-context.md``) would expose
       the order-dependent bug the canonical-preference rule was
       added to fix.

    Pre: ``candidates`` is non-empty (caller filtered empty slugs).
    """
    canonical_path = f"{_PROJECTS_PATH_PREFIX}{slug}/{slug}-project-context.md"
    for candidate in candidates:
        if candidate.path == canonical_path:
            return candidate
    return min(candidates, key=lambda c: c.path)


def _print_text_rows(rows: list[_ProjectRow]) -> None:
    """Render rows as an aligned three-column table to stdout.

    Empty result prints nothing (no header) so scripted callers
    piping into ``wc -l`` get a meaningful zero. The header is only
    rendered when there's at least one row to keep the output
    self-describing when the operator runs the command interactively.
    """
    if not rows:
        return
    slug_width = max(len("slug"), max(len(r.slug) for r in rows))
    status_width = max(len("status"), max(len(r.status or "—") for r in rows))
    typer.echo(f"{'slug':<{slug_width}}  {'status':<{status_width}}  local")
    for row in rows:
        status = row.status or "—"
        local_mark = (
            f"✓ ({row.repo})" if row.local and row.repo else "✓" if row.local else "✗"
        )
        typer.echo(f"{row.slug:<{slug_width}}  {status:<{status_width}}  {local_mark}")
