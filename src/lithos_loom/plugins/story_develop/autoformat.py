"""Auto-format-before-review pass (#134, ADR 0003 §4).

Immediately after the coder's commit, each detected ecosystem's formatter runs in
the sandbox in **write** mode; if it changes anything, the change is committed as a
**separate** commit on the round, and the deterministic gate + reviewer panel then
see that exact formatted tree. Formatting therefore always precedes the gate and the
panel, and loom **never** formats after approval (a post-approval format would
invalidate what the reviewers signed off).

Because formatting is applied deterministically up front, the profile's ``format``
check (the read-only ``ruff format --check`` form in :mod:`check_catalog`) should
always already be clean by the time it would run — it is required-but-non-blocking and
is not run as a standalone gate check yet (see ``develop._build_profile_checks``).

The pass is **best-effort**: a formatter that is absent from the image or errors out
is skipped with a warning rather than failing the run — the read-only ``format`` floor
(when it lands) and the reviewers remain the backstop. Layered like :mod:`test_gate`:
the worktree is mounted RW into the same hardened throwaway container, the formatter
rewrites source in place, and :func:`...runner.git.commit_all` captures the diff.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ...runner import detection, git
from . import check_catalog, containers, test_gate
from .config import HANDOFF_DIRNAME, DevelopConfig

logger = logging.getLogger(__name__)


def resolve_formatters(config: DevelopConfig, wt: Path) -> list[str]:
    """The runnable write-mode formatter commands for *wt*, probed once for the run.

    Detects the repo's ecosystem(s), maps each to its write-mode formatter
    (:func:`check_catalog.formatter_commands`), then keeps only those whose tool is
    present in the gate image (a single probe container, like the check-set). Empty for
    a markerless repo or an image with no formatter installed — the pass is then a
    no-op.
    """
    commands = [
        cmd
        for _eco, cmd in check_catalog.formatter_commands(
            detection.detect_ecosystems(wt)
        )
    ]
    if not commands:
        return []
    available = set(
        test_gate.probe_tools(config.image, [c.split()[0] for c in commands])
    )
    return [c for c in commands if c.split()[0] in available]


def run_format_pass(
    config: DevelopConfig,
    wt: Path,
    round_no: int,
    formatters: list[str],
) -> str | None:
    """Run *formatters* against the worktree and commit any change separately.

    Each formatter runs in its own throwaway container (the hardened gate profile)
    with the worktree mounted **RW** so it rewrites source in place. After all run, a
    single commit captures the cumulative formatting diff (``.handoff`` excluded, like
    the round commit). Returns the new commit SHA when formatting changed something,
    or ``None`` when nothing was reformatted (or no formatters ran). Best-effort: a
    per-formatter container failure is logged and skipped, never raised — the run
    proceeds against the unformatted (or partially-formatted) tree.
    """
    if not formatters:
        return None
    cache = config.gate_dir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    for command in formatters:
        name = containers.container_name(
            config.run_id, f"format-{command.split()[0]}-r{round_no}"
        )
        try:
            fmt_cmd = test_gate.build_gate_command(
                name=name,
                image=config.image,
                tree=wt,
                cache_dir=cache,
                command=command,
            )
            result = test_gate.run_gate_container(
                fmt_cmd, name=name, command=command, timeout=config.test_timeout
            )
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(
                "story-develop %s: round %d formatter `%s` errored (skipping): %s",
                config.run_id,
                round_no,
                command,
                exc,
            )
            continue
        logger.info(
            "story-develop %s: round %d formatter `%s` (exit %d)",
            config.run_id,
            round_no,
            command,
            result.exit_code,
        )
    format_sha = git.commit_all(
        wt,
        f"story-develop r{round_no}: auto-format",
        exclude=[HANDOFF_DIRNAME],
    )
    if format_sha is not None:
        logger.info(
            "story-develop %s: round %d auto-format committed %s",
            config.run_id,
            round_no,
            format_sha[:12],
        )
    return format_sha
