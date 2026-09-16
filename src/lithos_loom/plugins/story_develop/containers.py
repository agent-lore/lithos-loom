"""Per-agent container plumbing for ``story-develop``.

Two layers, deliberately split:

* the **pure builder** :func:`build_run_command` that returns the ``docker run``
  argv — unit-tested without Docker (the per-turn ``docker exec`` argv lives on
  :meth:`Engine.build_exec_argv`);
* **thin wrappers** (:func:`start_container`, :func:`exec_turn`,
  :func:`stop_container`) that actually shell out — monkeypatched in
  orchestration tests, exercised for real only in the integration test.

Design per ADR 0002 + the PRD: long-lived idle container (``sleep infinity``)
that we ``docker exec`` into per turn; hardened profile (``cap_drop: ALL``,
``no-new-privileges``); per-run ``CLAUDE_CONFIG_DIR`` with only the single auth
file bind-mounted in (RW; a refresh the container attempts cannot be relied on —
the mount pins the inode, #403 — so the reaction loops re-sync it from the host
before an auth retry) — never the whole ``~/.claude``.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

from .config import (
    CONTAINER_NOFILE_ULIMIT,
    CONTAINER_SHM_SIZE,
    HANDOFF_MOUNT_NAME,
    WORKSPACE_MOUNT,
)

logger = logging.getLogger(__name__)

#: One docker exec must never hang a retry on a dead daemon.
_RESYNC_TIMEOUT_S = 30


def container_name(run_id: str, agent: str) -> str:
    """Stable, unique-per-run container name, e.g. ``loom-develop-ab12cd34-coder``."""
    return f"loom-develop-{run_id}-{agent}"


def build_run_command(
    *,
    name: str,
    image: str,
    worktree: Path,
    config_dir: Path,
    handoff_dir: Path,
    config_mount: str,
    config_env_var: str,
    auth_source_dir: Path,
    auth_files: Sequence[str],
    skills_dir: Path | None = None,
    read_only_worktree: bool = False,
    git_common_dir: Path | None = None,
    artifacts_dir: Path | None = None,
) -> list[str]:
    """Build the ``docker run`` argv for a long-lived idle agent container.

    The container does nothing but ``sleep`` — turns are injected later via
    ``docker exec`` (:meth:`Engine.build_exec_argv`). This builder is
    **engine-blind**: the caller reads
    *config_mount* / *config_env_var* / *auth_source_dir* / *auth_files* /
    *skills_dir* off the :class:`Engine` (ARCH-2.E3), so a new tool needs no edit
    here.

    Mounts:

    * the worktree at ``/workspace`` (RW, or RO for reviewers);
    * *handoff_dir* at ``/workspace/.handoff`` (RW) — a separate dir outside the
      worktree, so the worktree stays git-clean;
    * *artifacts_dir* at ``/workspace/.handoff/artifacts`` (**RO**) when
      provided (#283) — collected gate-check artifacts (e2e screenshots). A
      nested mount shadowing any same-named path in the RW handoff: the host
      collector is the only writer, so an agent can neither forge artifacts
      nor plant a symlink destination for the host-privileged copy (PR #289
      review);
    * *config_dir* (per-run) at *config_mount* (RW, holds the transcript) —
      ``/claude_config`` exported as ``CLAUDE_CONFIG_DIR`` for claude,
      ``/codex_home`` exported as ``CODEX_HOME`` for codex (#94);
    * each of *auth_files* individually from *auth_source_dir* (RW, token
      refresh) — never the whole config dir;
    * *skills_dir* at ``<config-mount>/skills`` (RO) when provided, so
      operator-installed skills are available (feasibility gate G2). Codex has
      no skill concept, so codex agents pass ``skills_dir=None``.
    * *git_common_dir* at its identical host path (RO) when provided (#109), so
      a linked worktree's ``gitdir:`` backlink resolves in-container and
      reviewers can ``git diff``/``log``/``show`` the change.
    """
    workspace_mount = f"{worktree}:{WORKSPACE_MOUNT}"
    if read_only_worktree:
        workspace_mount += ":ro"

    cmd: list[str] = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--init",
        "--name",
        name,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--ulimit",
        f"nofile={CONTAINER_NOFILE_ULIMIT}",
        "--shm-size",
        CONTAINER_SHM_SIZE,
        "-v",
        workspace_mount,
        "-v",
        f"{handoff_dir}:{WORKSPACE_MOUNT}/{HANDOFF_MOUNT_NAME}",
    ]
    if artifacts_dir is not None:
        cmd += [
            "-v",
            f"{artifacts_dir}:{WORKSPACE_MOUNT}/{HANDOFF_MOUNT_NAME}/artifacts:ro",
        ]
    cmd += [
        "-v",
        f"{config_dir}:{config_mount}",
    ]
    for fname in auth_files:
        cmd += ["-v", f"{auth_source_dir / fname}:{config_mount}/{fname}"]
    if skills_dir is not None:
        cmd += ["-v", f"{skills_dir}:{config_mount}/skills:ro"]
    if git_common_dir is not None:
        # Linked-worktree git access (#109): the worktree's `.git` is a file
        # whose `gitdir:` backlink points at <repo>/.git/worktrees/<branch> by
        # absolute host path. Mount the common dir at that SAME path (identity
        # mount) so the backlink resolves and reviewers can `git diff`/`log`/
        # `show`. RO: loom commits host-side, so no agent needs write access to
        # the real repo's object store (and a --cap-drop ALL agent shouldn't).
        cmd += ["-v", f"{git_common_dir}:{git_common_dir}:ro"]
    cmd += ["-e", f"{config_env_var}={config_mount}"]
    cmd += ["--entrypoint", "sleep", image, "infinity"]
    return cmd


# --- thin side-effecting wrappers (monkeypatched in unit tests) -------------


def start_container(run_cmd: Sequence[str]) -> str:
    """Run ``docker run -d`` and return the container id (stdout)."""
    result = subprocess.run(list(run_cmd), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"docker run failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def exec_turn(
    exec_cmd: Sequence[str], *, timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run ``docker exec`` for one turn with stdin closed (no 3s stdin wait)."""
    return subprocess.run(
        list(exec_cmd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def resync_auth_files(
    name: str,
    *,
    config_mount: str,
    auth_source_dir: Path,
    auth_files: Sequence[str],
) -> list[str]:
    """Write the host's CURRENT auth files into the container's bind-mounted
    inodes, in place — only where the mount is demonstrably a STALE inode
    (#403).

    Each auth file is bind-mounted as a single file, which pins the inode at
    container start; the agent CLIs refresh their token by rename-replace, so
    a container alive across a host-side refresh keeps reading the OLD file —
    its access token expires and its refresh token was rotated away ("OAuth
    session expired and could not be refreshed" with a valid host file). A
    host-side copy or rename never reaches the mount; streaming the bytes
    through ``docker exec -i … cat > <mount>/<file>`` does (an in-place write).

    The write is guarded on INODE IDENTITY (PR #405 review): when no host
    refresh has happened the mount still IS the host's live file, and a write
    through it would rewrite the operator's live credentials — a byte
    comparison cannot prove non-aliasing (an in-place refresh between the two
    reads makes the bytes differ while the inode is shared, and the write
    would roll the live file back). So the container's inode of the target is
    compared with the inode of the host file whose bytes are about to be
    written (the same open fd — one snapshot); equal → aliased → never
    written. Also skipped: a host file that is empty or not JSON (the CLI
    mid-rewrite — never install garbage), a file the container never mounted
    (a candidate absent at start; writing would create a plaintext copy in
    the run dir), a dead / hung container (exec error or timeout — the retry
    then fails for real and escalates as before). Returns the files that
    landed.
    """
    synced: list[str] = []
    for fname in auth_files:
        target = f"{config_mount}/{fname}"
        try:
            with open(auth_source_dir / fname, "rb") as fh:
                host_inode = os.fstat(fh.fileno()).st_ino
                data = fh.read()
            json.loads(data)
        except (OSError, ValueError) as exc:
            logger.warning("auth re-sync: host %s unusable, skipped: %s", fname, exc)
            continue
        try:
            probe = subprocess.run(
                ["docker", "exec", name, "stat", "-c", "%i", target],
                capture_output=True,
                timeout=_RESYNC_TIMEOUT_S,
            )
            if probe.returncode != 0:
                logger.warning(
                    "auth re-sync: %s has no %s to refresh (rc %d), skipped",
                    name,
                    target,
                    probe.returncode,
                )
                continue
            if probe.stdout.strip() == str(host_inode).encode():
                # the mount still aliases the host's live file: nothing is
                # stale, and a write would go through to the operator's login
                continue
            write = f"cat > {shlex.quote(target)}"
            proc = subprocess.run(
                ["docker", "exec", "-i", name, "sh", "-c", write],
                input=data,
                capture_output=True,
                timeout=_RESYNC_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("auth re-sync of %s into %s failed: %s", fname, name, exc)
            continue
        if proc.returncode == 0:
            synced.append(fname)
        else:
            logger.warning(
                "auth re-sync of %s into %s failed (rc %d): %s",
                fname,
                name,
                proc.returncode,
                proc.stderr.decode(errors="replace").strip()[:200],
            )
    return synced


#: One liveness probe must never hang the failure path on a dead daemon.
_PROBE_TIMEOUT_S = 30


def container_running(name: str) -> bool | None:
    """Whether the container is running NOW (#412) — ``False`` when stopped or
    gone (a docker daemon restart removes ``--rm`` containers outright), and
    DELIBERATELY also when the daemon is unreachable (``docker inspect``
    fails with "Cannot connect to the Docker daemon"): mid-restart the
    container will not survive, and "not running" is what keeps the retry
    on the infra path. ``None`` only when the probe itself could not run
    (docker hung past the cap, or absent from PATH). Never raises: it runs
    on a turn's failure path, where a second failure must not mask the
    first."""
    try:
        proc = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("liveness probe of %s could not run: %s", name, exc)
        return None
    if proc.returncode != 0:
        return False
    return proc.stdout.strip().lower() == "true"


def stop_container(name: str) -> None:
    """Force-remove the container; never raises (teardown must be best-effort)."""
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True,
        text=True,
    )
