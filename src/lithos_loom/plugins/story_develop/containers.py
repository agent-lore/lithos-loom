"""Per-agent container plumbing for ``story-develop``.

Two layers, deliberately split:

* **pure builders** (:func:`build_run_command`, :func:`build_exec_command`) that
  return ``docker`` argv lists — unit-tested without Docker;
* **thin wrappers** (:func:`start_container`, :func:`exec_turn`,
  :func:`stop_container`) that actually shell out — monkeypatched in
  orchestration tests, exercised for real only in the integration test.

Design per ADR 0002 + the PRD: long-lived idle container (``sleep infinity``)
that we ``docker exec`` into per turn; hardened profile (``cap_drop: ALL``,
``no-new-privileges``); per-run ``CLAUDE_CONFIG_DIR`` with only the single auth
file bind-mounted in (RW, for token refresh) — never the whole ``~/.claude``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from . import engines
from .config import (
    CLAUDE_CONFIG_MOUNT,
    CODEX_CONFIG_MOUNT,
    CONTAINER_NOFILE_ULIMIT,
    HANDOFF_MOUNT_NAME,
    WORKSPACE_MOUNT,
    DevelopConfig,
)


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
    auth_source_dir: Path,
    auth_files: Sequence[str],
    skills_dir: Path | None = None,
    read_only_worktree: bool = False,
    tool: str = "claude",
    git_common_dir: Path | None = None,
) -> list[str]:
    """Build the ``docker run`` argv for a long-lived idle agent container.

    The container does nothing but ``sleep`` — turns are injected later via
    :func:`build_exec_command`.

    Mounts:

    * the worktree at ``/workspace`` (RW, or RO for reviewers);
    * *handoff_dir* at ``/workspace/.handoff`` (RW) — a separate dir outside the
      worktree, so the worktree stays git-clean;
    * *config_dir* (per-run) at the tool's config mount (RW, holds the
      transcript) — ``/claude_config`` exported as ``CLAUDE_CONFIG_DIR`` for
      claude, ``/codex_home`` exported as ``CODEX_HOME`` for codex (#94);
    * each of *auth_files* individually from *auth_source_dir* (RW, token
      refresh) — never the whole config dir;
    * *skills_dir* at ``<config-mount>/skills`` (RO) when provided, so
      operator-installed skills are available (feasibility gate G2). Codex has
      no skill concept, so codex agents pass ``skills_dir=None``.
    * *git_common_dir* at its identical host path (RO) when provided (#109), so
      a linked worktree's ``gitdir:`` backlink resolves in-container and
      reviewers can ``git diff``/``log``/``show`` the change.
    """
    config_mount, config_env = (
        (CODEX_CONFIG_MOUNT, "CODEX_HOME")
        if tool == "codex"
        else (CLAUDE_CONFIG_MOUNT, "CLAUDE_CONFIG_DIR")
    )

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
        "-v",
        workspace_mount,
        "-v",
        f"{handoff_dir}:{WORKSPACE_MOUNT}/{HANDOFF_MOUNT_NAME}",
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
    cmd += ["-e", f"{config_env}={config_mount}"]
    cmd += ["--entrypoint", "sleep", image, "infinity"]
    return cmd


def build_exec_command(
    *,
    name: str,
    tool: str,
    prompt: str,
    session_id: str,
    resume: bool = False,
    workdir: str = WORKSPACE_MOUNT,
    model: str | None = None,
    effort: str | None = None,
) -> list[str]:
    """Build the ``docker exec`` argv for one agent turn (coder or reviewer).

    Delegate to :meth:`Engine.build_exec_argv` — the per-tool argv (claude's
    ``--session-id`` / ``--output-format json`` vs codex's ``exec [resume …]
    --json``) lives on the engine now. Raises ``ValueError`` for an unknown
    *tool* (via :func:`engines.get_engine`). Kept until the turn path migrates
    to the engine directly (ARCH-2.E2).
    """
    return engines.get_engine(tool).build_exec_argv(
        name=name,
        prompt=prompt,
        session_id=session_id,
        resume=resume,
        workdir=workdir,
        model=model,
        effort=effort,
    )


def resolve_auth_files(
    config: DevelopConfig, candidates: Sequence[str], *, tool: str = "claude"
) -> list[str]:
    """Return the subset of *candidates* present in *tool*'s operator config dir.

    Behaviour-preserving shim: only the source-dir lookup moves to the engine
    (:meth:`Engine.auth_source_dir`); the *supplied* candidates are still what get
    filtered — so a caller passing a narrower/temporary list keeps its contract.
    The engine's own :meth:`Engine.auth_files` (which filters the engine's built-in
    candidate set) is the going-forward API; this shim is deleted when container
    provisioning migrates to the engine (ARCH-2.E3).
    """
    source = engines.get_engine(tool).auth_source_dir(config)
    return [f for f in candidates if (source / f).is_file()]


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


def stop_container(name: str) -> None:
    """Force-remove the container; never raises (teardown must be best-effort)."""
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True,
        text=True,
    )
