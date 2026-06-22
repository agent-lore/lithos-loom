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

**Isolation (like the gate, not the coder).** Repo-controlled formatter config can run
arbitrary code (a `prettier.config.js`, a repo-local plugin), so the formatter is
treated as untrusted: each runs against an **isolated ``git archive`` export** of the
coder's commit — never the live worktree — in a hardened container with
``--network none`` and a cache **separate** from the deterministic gate's, so it cannot
reach the orchestration trust channel (``.handoff`` lives only in the worktree, not the
export), poison the gate's package cache, or egress. Only when a formatter **exits
clean** are its changes applied back to the worktree's tracked files (success-gated):
a formatter that exits nonzero or times out may have left a partially-rewritten tree,
so its edits are discarded and never reach the gate or panel.

The pass is **best-effort**: an absent / erroring / nonzero formatter is skipped with a
warning rather than failing the run — the read-only ``format`` floor (when it lands) and
the reviewers remain the backstop. Layered like :mod:`test_gate`: pure command builder
(unit-tested without Docker) + the thin side-effecting wrapper it reuses
(:func:`test_gate.run_gate_container`, monkeypatched in orchestration tests).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ...runner import detection, git
from . import check_catalog, containers, test_gate
from .config import (
    CONTAINER_NOFILE_ULIMIT,
    HANDOFF_DIRNAME,
    WORKSPACE_MOUNT,
    DevelopConfig,
)
from .test_gate import CACHE_MOUNT

logger = logging.getLogger(__name__)


def build_format_command(
    *,
    name: str,
    image: str,
    tree: Path,
    cache_dir: Path,
    command: str,
) -> list[str]:
    """Build the one-shot ``docker run`` argv for one formatter run.

    Mirrors :func:`test_gate.build_gate_command`'s hardened profile (``cap-drop ALL``,
    ``no-new-privileges``, nofile ulimit) but is **stricter**, because the formatter is
    repo-controlled and runs *before* the deterministic gate (#134 review, ADR §4):

    - ``--network none`` — formatters need no network; deny egress (no exfiltration /
      SSRF path from a malicious formatter config or plugin).
    - *tree* is an **isolated ``git archive`` export**, not the live worktree, so a
      formatter cannot reach ``.handoff`` (absent from the export) or write the branch
      directly — the host applies only a *successful* formatter's diff back.
    - *cache_dir* is the **format-pass** cache, distinct from the gate's, so a formatter
      cannot poison the package cache the "independent" gate later trusts.
    """
    return [
        "docker",
        "run",
        "--rm",
        "--init",
        "--name",
        name,
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--ulimit",
        f"nofile={CONTAINER_NOFILE_ULIMIT}",
        "-v",
        f"{tree}:{WORKSPACE_MOUNT}",
        "-v",
        f"{cache_dir}:{CACHE_MOUNT}",
        "-e",
        f"RUFF_CACHE_DIR={CACHE_MOUNT}/ruff",
        "-e",
        f"npm_config_cache={CACHE_MOUNT}/npm",
        "-w",
        WORKSPACE_MOUNT,
        "--entrypoint",
        "sh",
        image,
        "-c",
        command,
    ]


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


def _within(root: Path, target: Path) -> bool:
    """Whether *target* resolves to a path inside *root* (write-side traversal guard).

    Resolves *target*'s deepest **existing** ancestor (following any symlinks) and
    checks it stays under ``root.resolve()``, so a pre-existing worktree symlink in a
    parent component cannot redirect a write outside the worktree (CWE-22/CWE-59).
    """
    root_r = root.resolve()
    p = target
    while not p.exists():
        if p.parent == p:
            return False
        p = p.parent
    return p.resolve().is_relative_to(root_r)


def _apply_formatted_tree(formatted: Path, baseline: Path, wt: Path) -> bool:
    """Copy back only the files *this* formatter changed vs *baseline*; report change.

    *formatted* is the formatter's mutated ``git archive`` export; *baseline* is a
    **pristine** export of the same ``HEAD`` (the formatter never touched it). A file is
    applied to *wt* only when it is **tracked** (present in *baseline*) **and** the
    formatter actually changed it (``formatted`` bytes ≠ ``baseline`` bytes) — so a
    later ecosystem's formatter, whose full-tree export re-derives the *original* of
    files it did not touch, cannot revert an earlier formatter's already-applied edits
    (correctness/f-002), and a formatter-added untracked path is never copied back.

    The formatter is untrusted, and this runs on the **host**, so symlinks are never
    followed: a ``formatted`` (or ``baseline``) entry that is a symlink is skipped —
    both ``is_file()`` and ``read_bytes()`` would otherwise resolve it in the host
    namespace and could read a host secret into the committed tree (CWE-59/CWE-200) —
    and the destination is skipped if it is a worktree symlink or resolves outside *wt*.
    Returns ``True`` iff at least one file's content changed.
    """
    changed = False
    for src in formatted.rglob("*"):
        # CWE-59: never follow a symlink a malicious formatter may have planted in the
        # export (e.g. `leak.py -> /proc/self/environ` or `-> ~/.config/gh/hosts.yml`).
        if src.is_symlink() or not src.is_file():
            continue
        rel = src.relative_to(formatted)
        base = baseline / rel
        # Apply only tracked paths the formatter changed (see docstring). A baseline
        # symlink is likewise never dereferenced.
        if base.is_symlink() or not base.is_file():
            continue
        new = src.read_bytes()
        if base.read_bytes() == new:
            continue
        dst = wt / rel
        # Defense in depth: don't write through a pre-existing worktree symlink or to a
        # path that escapes the worktree.
        if dst.is_symlink() or not _within(wt, dst):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(new)
        changed = True
    return changed


def run_format_pass(
    config: DevelopConfig,
    wt: Path,
    round_no: int,
    formatters: list[str],
) -> str | None:
    """Format the coder's commit in isolation and commit any change separately.

    Each formatter runs against its **own** ``git archive`` export of the worktree's
    ``HEAD`` (the coder's just-made commit) in a hardened, network-less throwaway
    container (:func:`build_format_command`). Only a formatter that **exits clean** has
    the files **it changed** applied back to the worktree
    (:func:`_apply_formatted_tree`, diffed against a pristine ``HEAD`` baseline so a
    polyglot repo's later formatter cannot revert an earlier one's edits) — a nonzero /
    timed-out run may have left a partially-rewritten tree, so its edits are discarded
    so the gate + panel never see them. After every formatter, one commit captures the
    cumulative diff (``.handoff`` excluded, like the round commit). Returns the new
    commit SHA when formatting changed something, or ``None`` when nothing was
    reformatted (or no formatters ran). Best-effort: a per-formatter export / container
    failure is logged and skipped, never raised — the run proceeds unformatted.
    """
    if not formatters:
        return None
    cache = config.gate_dir / "format_cache"
    cache.mkdir(parents=True, exist_ok=True)
    scratch_root = config.gate_dir / f"round_{round_no:02d}" / "format"
    # A pristine, never-formatted export of HEAD: the per-formatter diff baseline (so an
    # apply-back touches only files that formatter changed) and the tracked-path list.
    baseline = scratch_root / "_baseline"
    try:
        test_gate.export_tree(wt, "HEAD", baseline)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "story-develop %s: round %d auto-format baseline export errored "
            "(skipping pass): %s",
            config.run_id,
            round_no,
            exc,
        )
        return None
    applied = False
    for command in formatters:
        tool = command.split()[0]
        export = scratch_root / tool
        name = containers.container_name(config.run_id, f"format-{tool}-r{round_no}")
        try:
            test_gate.export_tree(wt, "HEAD", export)
            fmt_cmd = build_format_command(
                name=name,
                image=config.image,
                tree=export,
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
        if not result.passed:
            # Nonzero / timeout: the export may be partially rewritten (a formatter can
            # touch some files then fail on an invalid one, or be killed mid-write), so
            # discard its edits rather than commit a half-formatted tree (#134 review).
            logger.warning(
                "story-develop %s: round %d formatter `%s` exited %d — discarding its "
                "edits",
                config.run_id,
                round_no,
                command,
                result.exit_code,
            )
            continue
        if _apply_formatted_tree(export, baseline, wt):
            applied = True
        logger.info(
            "story-develop %s: round %d formatter `%s` (exit %d)",
            config.run_id,
            round_no,
            command,
            result.exit_code,
        )
    if not applied:
        return None
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
