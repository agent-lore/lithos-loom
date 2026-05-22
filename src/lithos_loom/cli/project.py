"""``lithos-loom project`` sub-app (Slice 3, US31 pulled forward).

Currently exposes only ``list``, which the Slice 3 capture-macro
Templater script uses to populate the project-autocomplete prompt.
US31's broader ``project list`` shape (status / KB-presence columns)
will land in Slice 4 when the project-context-projection subscription
gives us the upstream signal; for now this is the minimal slice-3 cut.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from lithos_loom.config import load_config
from lithos_loom.errors import LithosLoomError

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
        help="Output format: 'text' (one slug per line) or 'json' "
        "(array of slugs). The capture-macro Templater script uses "
        "'json' to feed tp.system.suggester.",
    ),
) -> None:
    """List the project slugs configured in the TOML ``[projects]`` table.

    Slugs come straight from ``cfg.projects.keys()``, sorted
    alphabetically for deterministic output. Empty ``[projects]``
    prints nothing (text) or ``[]`` (json) and exits 0.
    """
    try:
        cfg = load_config(config)
    except LithosLoomError as exc:
        typer.echo(f"lithos-loom: {exc}", err=True)
        sys.exit(1)

    slugs = sorted(cfg.projects)

    if output_format == _FORMAT_JSON:
        typer.echo(json.dumps(slugs))
        return
    if output_format == _FORMAT_TEXT:
        for slug in slugs:
            typer.echo(slug)
        return
    typer.echo(
        f"lithos-loom: unknown --format {output_format!r} "
        f"(expected one of: {_FORMAT_TEXT}, {_FORMAT_JSON})",
        err=True,
    )
    sys.exit(2)
