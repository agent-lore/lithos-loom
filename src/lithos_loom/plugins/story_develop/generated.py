"""PRD pr-reconciliation S4, the loom half: generated artifacts are
REGENERATED on a merge, never merged.

A repo that commits generated output (the guardrail kit's ``docs/generated``,
a codegen client, a snapshot) turns every genuinely disjoint pair of stories
into a merge conflict, and every conflict in such a file into a resolution
no generator would ever emit — textual union, "ours", "theirs" and a coder's
hand-merge alike are all wrong for the same reason (the PRD's own argument
against a union merge driver). git offers nothing that regenerates
correctly: a merge driver runs per file, mid-merge, before the sources are
combined; a post-merge hook never fires on the ``--no-commit`` merges loom's
intakes are. So the policy lives where the merges happen:

- the project declares ``generated_paths`` (repo-relative prefixes) and a
  ``regenerate_command`` (``develop_generated_paths`` /
  ``develop_regenerate_command`` in its context doc, or the CLI flags);
- loom's merges (:mod:`merge_gate`, PRD S3; :mod:`conflict_resolve`, PRD S5)
  take EITHER side of a conflict in those paths (:func:`take_base_side`)
  and, once the composed tree is whole, run the generator on it
  (:func:`regenerate`) in the gate container — the export of the index, the
  same isolation the check-set runs under — and copy the declared paths
  back, so the merge commit carries fresh outputs and the project's own
  drift check passes.

Nothing outside the declared paths is ever copied back: the generator's
side effects elsewhere are not the policy's to apply.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ...runner import git
from . import containers, test_gate
from .autoformat import within_tree
from .config import HANDOFF_DIRNAME, DevelopConfig
from .test_gate import GateResult

logger = logging.getLogger(__name__)

__all__ = [
    "RegenerateResult",
    "is_generated",
    "parse_generated_paths",
    "parse_regenerate_command",
    "partition_conflicts",
    "post_commit_regenerate",
    "regenerate",
    "take_base_side",
]

# The same seam the gate + auto-format pass use (monkeypatched in tests).
RunContainer = Callable[..., GateResult]


def parse_generated_paths(value: object, *, where: str) -> tuple[str, ...]:
    """Validate + normalise a ``generated_paths`` declaration, or ``()``.

    A list of repo-relative prefixes (a directory or a file): ``./`` and a
    trailing ``/`` are stripped, absolute paths and ``..`` rejected, duplicates
    dropped, and a prefix nested under another rejected (declare the outer
    one — a nested pair would make the sync's delete pass ambiguous). Shared
    by the project-metadata loader, the per-task override and the CLI so
    every surface rejects the same garbage identically. Raises ValueError.
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(
            f"{where}: generated_paths must be a list of repo-relative paths "
            f"(got {value!r})"
        )
    out: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(
                f"{where}: generated_paths entries must be non-empty strings "
                f"(got {raw!r})"
            )
        text = raw.strip()
        if text.startswith("/"):
            raise ValueError(
                f"{where}: generated_paths must be repo-relative (got {text!r})"
            )
        parts = [p for p in PurePosixPath(text).parts if p not in ("", ".")]
        if not parts or ".." in parts:
            raise ValueError(
                f"{where}: generated_paths entry {text!r} must name a path inside "
                "the repo (no '..', not the repo root)"
            )
        norm = "/".join(parts)
        if norm not in out:
            out.append(norm)
    for a in out:
        for b in out:
            if a != b and is_generated(b, (a,)):
                raise ValueError(
                    f"{where}: generated_paths {b!r} is inside {a!r} — declare the "
                    "outer path only"
                )
    return tuple(out)


def parse_regenerate_command(value: object, *, where: str) -> str | None:
    """Validate a ``regenerate_command``, or ``None``. Mirrors
    ``parse_parity_command``: a non-empty string, trusted as-is (it runs in
    the gate container); a bad command surfaces when the container runs it."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{where}: regenerate_command must be a non-empty string (got {value!r})"
        )
    return value.strip()


def is_generated(path: str, prefixes: Sequence[str]) -> bool:
    """Whether *path* is one of the declared prefixes or lies under one
    (segment-wise: ``docs/generated`` covers ``docs/generated/x``, never
    ``docs/generated2/x``)."""
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def partition_conflicts(
    paths: Sequence[str], prefixes: Sequence[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split conflicted *paths* into ``(generated, real)``, order kept."""
    generated = tuple(p for p in paths if is_generated(p, prefixes))
    real = tuple(p for p in paths if not is_generated(p, prefixes))
    return generated, real


def take_base_side(wt: Path, paths: Sequence[str]) -> None:
    """Resolve conflicted generated *paths* of the in-progress merge to the
    base's copy and stage them. Which side is immaterial — the generator
    overwrites the content — but the base's is the one a clean merge would
    have carried, so the diff the panel later reads stays honest."""
    git.take_their_side(wt, paths)


@dataclass(frozen=True)
class RegenerateResult:
    """What :func:`regenerate` did. ``changed`` lists the declared-path files
    whose content the generator moved (added, rewritten or deleted), now
    staged; ``exit_code`` is ``None`` when the container never ran."""

    ok: bool
    exit_code: int | None
    output_tail: str
    changed: tuple[str, ...] = ()
    error: str = ""
    timed_out: bool = False


def regenerate(
    config: DevelopConfig,
    wt: Path,
    *,
    label: str,
    run_container: RunContainer = test_gate.run_gate_container,
) -> RegenerateResult:
    """Run the project's generator on the COMPOSED tree and copy the declared
    paths back into *wt*, staged.

    The composed tree is the index (:func:`git.write_tree`) — with a merge in
    progress, the auto-merged paths plus whatever was resolved and staged —
    exported with ``git archive`` and mounted into the gate container
    (:func:`test_gate.build_gate_command`: the gate's own image, cache and
    hardening), never the live worktree. Only a generator that exits 0 has
    its output applied, and only under ``config.generated_paths``: files are
    added, rewritten and deleted to match the export; symlinks are never
    followed on either side; a path that resolves outside *wt* is skipped.
    Never raises for the generator's own failure — an export or container
    error is a failed result with ``error`` set.
    """
    if not config.generated_paths or not config.regenerate_command:
        raise ValueError(
            "regenerate needs both generated_paths and regenerate_command declared"
        )
    command = config.regenerate_command
    scratch = config.gate_dir / "regenerate"
    export = scratch / f"{label}-{uuid.uuid4().hex}"
    cache = config.gate_dir / "cache"
    name = containers.container_name(config.run_id, f"regenerate-{label}")
    try:
        cache.mkdir(parents=True, exist_ok=True)
        tree_sha = git.write_tree(wt)
        test_gate.export_tree(wt, tree_sha, export)
        cmd = test_gate.build_gate_command(
            name=name, image=config.image, tree=export, cache_dir=cache, command=command
        )
        result = run_container(
            cmd, name=name, command=command, timeout=config.test_timeout
        )
        if not result.passed:
            logger.warning(
                "story-develop %s: regenerate (%s) `%s` exited %s — output discarded",
                config.run_id,
                label,
                command,
                result.exit_code,
            )
            return RegenerateResult(
                ok=False,
                exit_code=result.exit_code,
                output_tail=result.output_tail,
                timed_out=result.timed_out,
            )
        # the sync and the staging are the generator's effect on the tree —
        # a failure there (a file/dir shape change, a path git cannot stage)
        # is the generator's failure, never the caller's crash
        changed = tuple(_sync_generated(export, wt, config.generated_paths))
        git.stage_paths(wt, changed)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "story-develop %s: regenerate (%s) errored: %s", config.run_id, label, exc
        )
        return RegenerateResult(
            ok=False, exit_code=None, output_tail="", error=str(exc)
        )
    finally:
        _remove_export(config, label, export)
    logger.info(
        "story-develop %s: regenerate (%s) `%s` (exit %d): %d path(s) moved",
        config.run_id,
        label,
        command,
        result.exit_code,
        len(changed),
    )
    return RegenerateResult(
        ok=True,
        exit_code=result.exit_code,
        output_tail=result.output_tail,
        changed=changed,
    )


def post_commit_regenerate(
    config: DevelopConfig,
    *,
    run_container: RunContainer = test_gate.run_gate_container,
) -> Callable[[Path, int], str | None] | None:
    """The round's post-commit pass for a loop that composes trees (the S5
    resolve mode): after the coder's commit (and the auto-format pass), run
    the generator on HEAD and commit what it moved as its own commit —
    ``story-develop r<n>: regenerate`` — so the gate and the panel judge a
    tree whose generated output is the generator's, whatever the coder ran.
    ``None`` when the project declares no policy. Best-effort like the
    format pass: a generator that fails leaves the tree as committed (the
    project's drift check, where it has one, is the backstop) and is logged.
    """
    if not config.generated_paths or not config.regenerate_command:
        return None

    def run(wt: Path, round_no: int) -> str | None:
        result = regenerate(
            config, wt, label=f"r{round_no}", run_container=run_container
        )
        if not result.ok or not result.changed:
            return None
        return git.commit_all(
            wt, f"story-develop r{round_no}: regenerate", exclude=[HANDOFF_DIRNAME]
        )

    return run


def _remove_export(config: DevelopConfig, label: str, export: Path) -> None:
    """Remove the export; an undeletable remnant (root-owned files from a
    gate image without ``--user``) is logged, never raised — like the gate's."""
    if not export.exists():
        return
    try:
        shutil.rmtree(export)
    except OSError as exc:
        logger.warning(
            "story-develop %s: regenerate (%s) export dir not cleaned (%s): %s",
            config.run_id,
            label,
            export,
            exc,
        )


def _files_under(root: Path, prefix: str) -> dict[str, Path]:
    """Regular files under *root/prefix* by repo-relative path; symlinks (and
    anything under a symlinked directory) are never followed."""
    top = root / prefix
    found: dict[str, Path] = {}
    if top.is_symlink():
        return found
    if top.is_file():
        found[prefix] = top
        return found
    if not top.is_dir():
        return found
    for path in sorted(top.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        # a symlinked directory between `top` and the file is never followed
        # (rglob does not descend into one today; this keeps it so)
        between = [top / p for p in path.relative_to(top).parents if str(p) != "."]
        if any(a.is_symlink() for a in between):
            continue
        rel = path.relative_to(root).as_posix()
        found[rel] = path
    return found


def _sync_generated(export: Path, wt: Path, prefixes: Sequence[str]) -> list[str]:
    """Make *wt*'s declared paths match the *export*'s; return what moved."""
    changed: list[str] = []
    for prefix in prefixes:
        fresh = _files_under(export, prefix)
        stale = _files_under(wt, prefix)
        for rel, src in fresh.items():
            dst = wt / rel
            if dst.is_symlink() or not within_tree(wt, dst):
                continue
            new = src.read_bytes()
            if dst.is_file() and dst.read_bytes() == new:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(new)
            changed.append(rel)
        for rel, path in stale.items():
            if rel in fresh or path.is_symlink() or not within_tree(wt, path):
                continue
            path.unlink()
            changed.append(rel)
    return changed
