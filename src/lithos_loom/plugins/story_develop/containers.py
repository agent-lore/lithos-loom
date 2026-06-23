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

    ``--session-id`` controls the session on the first turn; ``--resume`` reloads
    it on later turns (T3). Output is ``--output-format json`` so completion /
    cost / errors come from structured output, not pane scraping.

    *model* / *effort*, when set, add ``--model <model>`` / ``--effort <level>``
    (#93) — passed on every turn, including resumes. *effort* is a Claude
    reasoning level (``low``…``max``), not a token budget; ``None`` leaves the
    agent default.

    Codex (#94) is the per-tool translation point: it takes ``model`` via
    ``-m`` but has no shared effort knob (codex depth is model-driven), so
    *effort* is ignored for codex. The session handle is **minted by the tool**
    on the first turn (``thread_id`` from the ``thread.started`` ``--json``
    event), not supplied — so on the first turn *session_id* is unused, and on
    resume it is passed positionally to ``codex exec resume``. ``--json`` emits
    JSONL; ``--dangerously-bypass-approvals-and-sandbox`` is the codex analogue
    of claude's ``--dangerously-skip-permissions`` (the container is the
    sandbox).
    """
    if tool == "claude":
        session_flag = (
            ["--resume", session_id] if resume else ["--session-id", session_id]
        )
        model_flag = ["--model", model] if model else []
        effort_flag = ["--effort", effort] if effort else []
        return [
            "docker",
            "exec",
            "-w",
            workdir,
            name,
            "claude",
            *session_flag,
            *model_flag,
            *effort_flag,
            "-p",
            "--dangerously-skip-permissions",
            "--output-format",
            "json",
            prompt,
        ]

    if tool == "codex":
        # Verified against codex-cli 0.139.0:
        #   first:  codex exec [OPTIONS] [PROMPT]
        #   resume: codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]
        # so the thread_id is the first positional after `resume` and the
        # prompt is the trailing positional (handle captured from turn 1's
        # `thread.started` event). The working dir is set by `docker exec -w`,
        # so the `-C/--cd` flag that `resume` lacks is not needed.
        subcommand = ["exec", "resume", session_id] if resume else ["exec"]
        model_flag = ["-m", model] if model else []  # effort is model-driven
        return [
            "docker",
            "exec",
            "-w",
            workdir,
            name,
            "codex",
            *subcommand,
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            *model_flag,
            prompt,
        ]

    raise ValueError(f"unsupported tool: {tool!r} (expected 'claude' or 'codex')")


def resolve_auth_files(
    config: DevelopConfig, candidates: Sequence[str], *, tool: str = "claude"
) -> list[str]:
    """Return the subset of *candidates* that exist in *tool*'s operator config dir.

    Reads ``~/.codex`` for codex, ``~/.claude`` for claude (#94) — see
    :meth:`DevelopConfig.auth_source_dir`.
    """
    source = config.auth_source_dir(tool)
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
