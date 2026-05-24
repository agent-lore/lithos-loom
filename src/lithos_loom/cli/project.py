"""``lithos-loom project`` sub-app (Slice 3 capture-macro helper).

This is **not** a US31 implementation, despite living in the
namespace US31 will eventually own. The full US31
(``docs/prd/integration.md:195``) enumerates Lithos project context
docs via ``lithos_list(path_prefix="projects/")`` and renders
status + presence-in-local-TOML columns — that depends on the
Slice 4 project-context-projection subscription giving us the
upstream signal, so it can't fully land until then.

What this command DOES ship is the minimum the Slice 3 capture
macro needs: enumerate the slugs in the operator's local TOML
``[projects]`` table so the macro's project-autocomplete prompt has
something to suggest. Local-config-only; no Lithos round-trip.

When US31 lands in Slice 4, this command's surface extends: same
sub-app, same ``list`` verb, but the output gains the Lithos-side
columns and the source of truth becomes a union of local TOML
and ``lithos_list``.
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
