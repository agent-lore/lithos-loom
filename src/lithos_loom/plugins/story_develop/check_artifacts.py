"""Gate-check artifact collection (#283 slice 1).

Rescues a check's rendered artifacts (e.g. e2e screenshots) from its doomed
per-check tree export into the run's host-controlled artifacts dir, as an
exact, symlink-free snapshot. See :func:`collect_check_artifacts` for the
threat model — the export's contents and the handoff are both untrusted, so
the host-privileged copy neither follows symlinks nor writes anywhere an
agent container can reach read-write.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .config import HANDOFF_MOUNT_NAME, WORKSPACE_MOUNT, DevelopConfig

logger = logging.getLogger(__name__)

# Provenance manifest written at the root of every published check snapshot
# (793edc9f): which COMMIT these pixels were rendered from. Published
# atomically with the files (staged into the same tmp dir before the rename)
# so manifest and pixels can never disagree; the copy loop refuses to copy a
# repo file that would land at this exact path, so a repo cannot forge
# provenance. Reviewers may open it (the mount is read-only); it is excluded
# from the note's listings and counts.
CAPTURE_MANIFEST = ".capture.json"


def _raise_walk_error(exc: OSError) -> None:
    """``os.walk`` swallows traversal errors by default — an unreadable nested
    dir would silently publish a PARTIAL snapshot logged as success (PR #289
    round 2). Raising routes it to the failure path: nothing published."""
    raise exc


def collect_check_artifacts(
    config: DevelopConfig, tree: Path, round_no: int, check_name: str, *, sha: str
) -> None:
    """Rescue a check's artifacts dir from its doomed tree export (#283).

    The per-check export is deleted right after the check runs (#282
    isolation), destroying anything the check rendered there — e.g. the e2e
    screenshots a repo-parity ``make e2e`` writes. When the project declares
    ``develop_artifacts_path``, snapshot that dir into the HOST-CONTROLLED
    ``config.artifacts_dir`` under ``round_NN/<check>/`` (agents see it via a
    read-only mount at ``.handoff/artifacts`` — never a host-privileged write
    into an agent-writable dir). Unconditional on the check's verdict — a RED
    e2e run's screenshots are exactly what a reviewer needs.

    Hardening (PR #289 review): the export's contents are repo/check-
    controlled, so the copy NEVER follows symlinks — the artifacts root must
    be a real directory resolving inside the export, and only regular,
    non-symlink files are copied (anything else is skipped and counted); a
    traversal error fails the whole collection rather than publishing a
    partial snapshot as success.

    Snapshot contract: after a SUCCESSFUL pass, ``round_NN/<check>`` reflects
    exactly this execution — files staged in a temp dir are published over any
    prior snapshot (rmtree + rename: a brief missing-destination window, fine
    for the sequential panel workflow — not a stronger atomicity claim), and
    an execution that produced nothing RETIRES the prior snapshot, so
    reviewers never mistake an older execution's artifacts for the current
    one. After a FAILED pass the prior snapshot is left untouched — its state
    is "unknown", not "no artifacts" — and the failure is logged, never
    fatal, never blocking the tree cleanup that follows.

    ``sha`` is the commit the exported *tree* was cut from; it is written into
    the snapshot's :data:`CAPTURE_MANIFEST` so the artifact pass can tell a
    capture of the tree under review from an earlier round's (793edc9f).
    """
    if not config.artifacts_path:
        return
    src = tree / config.artifacts_path
    dest = config.artifacts_dir / f"round_{round_no:02d}" / check_name
    tmp: Path | None = None
    try:
        if src.is_symlink() or not src.is_dir():
            _retire_prior(dest)
            return
        if not src.resolve().is_relative_to(tree.resolve()):
            logger.warning(
                "story-develop %s: round %d %s artifacts root resolves outside "
                "the export (skipping): %s",
                config.run_id,
                round_no,
                check_name,
                src,
            )
            _retire_prior(dest)
            return
        config.artifacts_dir.mkdir(parents=True, exist_ok=True)
        tmp = config.artifacts_dir / f".tmp-{check_name}-{uuid.uuid4().hex}"
        copied = 0
        skipped = 0
        for dirpath, dirnames, filenames in os.walk(
            src, onerror=_raise_walk_error, followlinks=False
        ):
            rel = Path(dirpath).relative_to(src)
            for fname in filenames:
                entry = Path(dirpath) / fname
                if entry.is_symlink() or not entry.is_file():
                    skipped += 1
                    continue
                if (rel / fname).as_posix() == CAPTURE_MANIFEST:
                    # the provenance slot is loom's, not the repo's
                    skipped += 1
                    continue
                target_dir = tmp / rel
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry, target_dir / fname)
                copied += 1
            # prune symlinked subdirs from the walk (followlinks=False stops
            # descent, but they'd still be listed; count them as skipped)
            links = [d for d in dirnames if (Path(dirpath) / d).is_symlink()]
            skipped += len(links)
            dirnames[:] = [d for d in dirnames if d not in links]
        if copied == 0:
            _retire_prior(dest)
            return
        (tmp / CAPTURE_MANIFEST).write_text(
            json.dumps(
                {
                    "sha": sha,
                    "round": round_no,
                    "check": check_name,
                    "captured_at": datetime.now(UTC).isoformat(),
                    "files": copied,
                }
            ),
            encoding="utf-8",
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        os.rename(tmp, dest)
        tmp = None
        logger.info(
            "story-develop %s: round %d %s artifacts collected to %s (%d files%s)",
            config.run_id,
            round_no,
            check_name,
            dest,
            copied,
            f", {skipped} non-regular entries skipped" if skipped else "",
        )
    except OSError as exc:
        logger.warning(
            "story-develop %s: round %d %s artifact collection failed (continuing): %s",
            config.run_id,
            round_no,
            check_name,
            exc,
        )
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def _retire_prior(dest: Path) -> None:
    """An execution that produced no artifacts must not leave a previous
    execution's snapshot posing as current (PR #289 round 2)."""
    if dest.exists():
        shutil.rmtree(dest)


def read_capture_manifest(check_dir: Path) -> dict[str, Any] | None:
    """The snapshot's provenance manifest, or ``None`` when absent/corrupt.

    ``None`` means UNKNOWN provenance — pre-manifest snapshots (a run resumed
    across a loom upgrade) or a damaged file. Callers treat unknown as stale:
    fail closed, never "assume current".
    """
    path = check_dir / CAPTURE_MANIFEST
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def capture_freshness(
    config: DevelopConfig, sha: str | None
) -> Literal["no_artifacts", "current", "stale"]:
    """Classify the artifacts dir against the tree under review (793edc9f).

    ``no_artifacts``: no snapshot holds any reviewable file — nothing to
    falsely verify. ``current``: at least one snapshot's manifest records
    ``sha``. ``stale``: snapshots exist but none is from ``sha`` (covers a
    skipped/failed re-capture leaving an older round's dir newest, missing
    manifests, and ``sha is None``) — reviewing those pixels as this tree's
    would be a silent false-verify, the class RH-3 exists to prevent.
    """
    root = config.artifacts_dir
    if not root.is_dir():
        return "no_artifacts"
    seen_files = False
    for round_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for check_dir in sorted(p for p in round_dir.iterdir() if p.is_dir()):
            if not any(
                f.is_file() and f.relative_to(check_dir).as_posix() != CAPTURE_MANIFEST
                for f in check_dir.rglob("*")
            ):
                continue
            seen_files = True
            manifest = read_capture_manifest(check_dir)
            if sha is not None and manifest is not None and manifest.get("sha") == sha:
                return "current"
    return "stale" if seen_files else "no_artifacts"


def stale_capture_hold(
    config: DevelopConfig,
    *,
    gated_sha: str | None,
    check_results: tuple[Any, ...],
    round_no: int,
) -> str | None:
    """The approval-hold notice when artifact evidence is stale, else ``None``.

    793edc9f: called by ``rounds.approval_phase`` once every reviewer passed.
    One trigger: :func:`capture_freshness` says ``stale`` — snapshots exist,
    none from ``gated_sha`` (a skipped candidate run, an empty/failed/ERRORED
    re-capture, missing manifests). ``no_artifacts`` never holds — a repo
    whose ``artifacts_path`` legitimately produces nothing must not livelock —
    and ``current`` proceeds to the artifact pass.

    An errored capture check is deliberately NOT its own trigger (PR #340
    review): it is subsumed — an errored check publishes no snapshot, so the
    sealing sha has no current capture and the freshness classification holds
    by itself (T1-S11's round 5: parity errored, ``round_04`` stayed newest →
    ``stale``). Triggering on *any* errored candidate check would falsely
    hold runs whose candidate set carries non-artifact checks (thorough's
    dep-audit / coverage / semgrep), including errored results an earlier
    round's ``merge_check_sets`` carried forward. Errored candidate checks
    are still NAMED in the notice as the likely cause when stale. Residual,
    accepted: a first-round errored capture with no snapshots at all
    classifies ``no_artifacts`` and seals — the floor's errored-passes
    semantics are out of scope here.

    The returned notice is coder-facing (appended to the next round's gate
    summary): it names the stale commit and the failed capture so the loop
    can fix it. A capture that stays broken walks to ``stalled``/``max_rounds``
    with the reason on record — the honest outcome replacing the silent
    false-verify. The hold is also logged loudly here.
    """
    if not config.artifacts_path:
        return None
    if capture_freshness(config, gated_sha) != "stale":
        return None
    errored = [
        r.check.name
        for r in check_results
        if r.check.stage == "candidate"
        and r.execution_outcome in ("errored", "timed_out")
    ]
    sha12 = (gated_sha or "?")[:12]
    cause = (
        "no artifact snapshot was captured from this commit — the newest "
        "captures pre-date it"
    )
    if errored:
        cause += (
            f" (candidate check(s) {', '.join(errored)} errored, "
            "likely why no fresh capture exists)"
        )
    logger.warning(
        "story-develop %s: round %d reviews passed but the artifact capture "
        "is stale for %s (%s); holding approval",
        config.run_id,
        round_no,
        sha12,
        cause,
    )
    return (
        "## Artifact capture is stale — approval is held\n\n"
        f"All reviewers passed, but for commit {sha12} {cause}. Rendered-output "
        "findings cannot be verified against an earlier round's screenshots, "
        "so approval is held until a capture from the current tree exists. "
        "Investigate why the capture produced nothing (check "
        f"`{config.artifacts_path}` and the candidate check's own output) "
        "and fix that — a run whose capture stays broken will stall rather "
        "than approve unseen."
    )


# How many filenames to spell out per check dir in the reviewer note; the rest
# collapse to "+N more" (a 4-page x 4-width screenshot matrix is 16 files —
# enumerable, but a long suite must not drown the prompt).
_NOTE_FILES_PER_CHECK = 12
# Total listed-file budget across ALL rounds/checks (PR #291 review): per-check
# caps alone let many rounds x checks grow every reviewer prompt unboundedly.
# Spent newest-round-first — the current round's rendering is what approval
# hinges on; older dirs beyond the budget stay enumerated as count-only lines.
_NOTE_TOTAL_FILE_BUDGET = 36


def render_artifacts_note(config: DevelopConfig) -> str:
    """The reviewer-prompt section enumerating collected artifacts (#283 s2).

    Empty string when nothing was collected (the template slot renders blank).
    Paths are the IN-CONTAINER view — ``/workspace/.handoff/artifacts/…`` (the
    read-only mount) — because the reader is an agent inside the container,
    not the host. Nested files keep their check-relative paths (openable as
    listed, and duplicate basenames stay distinguishable). Rounds are listed
    newest first; a total listed-file budget applies across the whole note,
    with per-dir listings additionally capped — dirs beyond the budget render
    as count-only lines so nothing is silently hidden.

    The :data:`CAPTURE_MANIFEST` provenance file is plumbing, not a
    reviewable artifact — excluded from listings and counts, so the rendered
    note is byte-identical to the pre-manifest surface. Reviewer-visible
    CURRENT/PRIOR provenance labels were built and then SPLIT OUT of PR #340:
    on the RH-3 measured surface the labelled arm read 5/8 pooled catch
    against the shipped prompt's 9/10 historical, so the labels + prompt
    truth-fix are deferred to their own control+arm measured lever. The
    stale-capture false-verify is closed code-side by
    :func:`stale_capture_hold` regardless.
    """
    root = config.artifacts_dir
    if not root.is_dir():
        return ""
    lines: list[str] = []
    remaining = _NOTE_TOTAL_FILE_BUDGET
    for round_dir in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        for check_dir in sorted(p for p in round_dir.iterdir() if p.is_dir()):
            files = sorted(
                rel
                for p in check_dir.rglob("*")
                if p.is_file()
                and (rel := p.relative_to(check_dir).as_posix()) != CAPTURE_MANIFEST
            )
            if not files:
                continue
            mount = (
                f"{WORKSPACE_MOUNT}/{HANDOFF_MOUNT_NAME}/artifacts/"
                f"{round_dir.name}/{check_dir.name}"
            )
            cap = min(_NOTE_FILES_PER_CHECK, remaining)
            shown = files[:cap]
            remaining -= len(shown)
            if shown:
                extra = len(files) - len(shown)
                listing = ", ".join(shown) + (f", +{extra} more" if extra > 0 else "")
                lines.append(f"- `{mount}/` — {len(files)} file(s): {listing}")
            else:
                lines.append(
                    f"- `{mount}/` — {len(files)} file(s) "
                    "(listing omitted — prompt budget; older round)"
                )
    if not lines:
        return ""
    return (
        "## Rendered-page artifacts\n"
        "\n"
        "Gate checks captured rendered output from this run — screenshots of "
        "the application's actual pages. **Open and look at these image files** "
        "(they are readable in-container at the paths below) and evaluate the "
        "rendered result alongside the diff: layout and hierarchy at each "
        "width, interaction and degraded states, obvious visual breakage. "
        "Artifacts from a RED e2e check show the failing state.\n"
        "\n" + "\n".join(lines) + "\n"
    )
