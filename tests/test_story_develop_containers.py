"""Unit tests for the pure docker command builders."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import containers, engines
from lithos_loom.plugins.story_develop.config import (
    CONTAINER_NOFILE_ULIMIT,
    CONTAINER_SHM_SIZE,
)


def _exec_cmd(
    *,
    name: str,
    tool: str,
    prompt: str,
    session_id: str,
    resume: bool = False,
    model: str | None = None,
    effort: str | None = None,
) -> list[str]:
    # containers.build_exec_command (the tool->engine delegate) was removed at
    # ARCH-2.E5; the engine owns the docker-exec argv. This helper keeps the
    # tool->engine pick local to the tests that pin that argv.
    return engines.get_engine(tool).build_exec_argv(
        name=name,
        prompt=prompt,
        session_id=session_id,
        resume=resume,
        model=model,
        effort=effort,
    )


def _run_cmd(**over) -> list[str]:
    # config_mount / config_env_var are engine-supplied now (ARCH-2.E3); the
    # claude defaults keep these argv assertions byte-identical.
    kwargs: dict = dict(
        name="loom-develop-ab12cd34-coder",
        image="ralph-sandbox:latest",
        worktree=Path("/work/run/worktree/branch"),
        config_dir=Path("/work/run/agents/coder/claude_config"),
        handoff_dir=Path("/work/run/handoff"),
        config_mount="/claude_config",
        config_env_var="CLAUDE_CONFIG_DIR",
        auth_source_dir=Path("/home/u/.claude"),
        auth_files=[".credentials.json"],
    )
    kwargs.update(over)
    return containers.build_run_command(**kwargs)


def test_container_shm_size_is_one_gigabyte() -> None:
    """The shared-memory policy is specifically 1g (chromium needs well over
    Docker's 64m default) — propagation tests alone would pass with any value."""
    assert CONTAINER_SHM_SIZE == "1g"


def test_artifacts_dir_mounts_read_only_nested_under_handoff() -> None:
    """#283: collected artifacts are host-written and agent-READ-ONLY — the
    nested mount shadows any same-named path an agent creates in the RW
    handoff, closing the planted-symlink destination route (PR #289)."""
    cmd = containers.build_run_command(
        name="c",
        image="img",
        worktree=Path("/w"),
        config_dir=Path("/cfg"),
        handoff_dir=Path("/h"),
        config_mount="/claude_config",
        config_env_var="CLAUDE_CONFIG_DIR",
        auth_source_dir=Path("/auth"),
        auth_files=(),
        artifacts_dir=Path("/run/artifacts"),
    )
    assert "/run/artifacts:/workspace/.handoff/artifacts:ro" in cmd
    # ordering: docker applies nested binds by target depth, but the RW handoff
    # mount must still be present alongside
    assert "/h:/workspace/.handoff" in cmd


def test_run_command_hardened_profile_and_mounts() -> None:
    cmd = _run_cmd()
    assert cmd[:3] == ["docker", "run", "-d"]
    assert "--rm" in cmd and "--init" in cmd
    # hardened
    assert cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges:true" in cmd
    assert cmd[cmd.index("--ulimit") + 1] == f"nofile={CONTAINER_NOFILE_ULIMIT}"  # #117
    assert cmd[cmd.index("--shm-size") + 1] == CONTAINER_SHM_SIZE  # browser e2e
    # worktree RW, handoff dir OUTSIDE the worktree, config dir, single auth file
    assert "/work/run/worktree/branch:/workspace" in cmd
    assert "/work/run/handoff:/workspace/.handoff" in cmd
    assert "/work/run/agents/coder/claude_config:/claude_config" in cmd
    assert "/home/u/.claude/.credentials.json:/claude_config/.credentials.json" in cmd
    assert "CLAUDE_CONFIG_DIR=/claude_config" in cmd
    # idle entrypoint with the image and arg trailing
    assert cmd[-4:] == ["--entrypoint", "sleep", "ralph-sandbox:latest", "infinity"]


def test_run_command_mounts_skills_read_only_when_present() -> None:
    cmd = _run_cmd(skills_dir=Path("/home/u/.claude/skills"))
    assert "/home/u/.claude/skills:/claude_config/skills:ro" in cmd


def test_run_command_omits_skills_when_absent() -> None:
    cmd = _run_cmd(skills_dir=None)
    assert not any(a.endswith(":/claude_config/skills:ro") for a in cmd)


def test_run_command_readonly_worktree() -> None:
    cmd = _run_cmd(read_only_worktree=True)
    assert "/work/run/worktree/branch:/workspace:ro" in cmd


def test_run_command_mounts_git_common_dir() -> None:
    cmd = _run_cmd(git_common_dir=Path("/home/project/.git"))
    # identity mount (#109): host path == container path, read-only, so the
    # worktree's absolute `gitdir:` backlink resolves in-container.
    assert "/home/project/.git:/home/project/.git:ro" in cmd


def test_run_command_omits_git_common_dir_when_absent() -> None:
    cmd = _run_cmd()  # git_common_dir defaults to None
    assert not any(a.endswith("/.git:ro") for a in cmd)


def test_run_command_multiple_auth_files() -> None:
    cmd = _run_cmd(auth_files=[".credentials.json", ".claude.json"])
    assert "/home/u/.claude/.claude.json:/claude_config/.claude.json" in cmd


def test_run_command_no_auth_files() -> None:
    cmd = _run_cmd(auth_files=[])
    assert not any(":/claude_config/." in a for a in cmd)


def test_exec_command_first_turn_uses_session_id() -> None:
    cmd = _exec_cmd(name="c", tool="claude", prompt="do it", session_id="sid-1")
    assert cmd[:5] == ["docker", "exec", "-w", "/workspace", "c"]
    assert "claude" in cmd
    assert cmd[cmd.index("--session-id") + 1] == "sid-1"
    assert "-p" in cmd and "--dangerously-skip-permissions" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[-1] == "do it"  # prompt passed as a single argv element


def test_exec_command_resume_uses_resume_flag() -> None:
    cmd = _exec_cmd(
        name="c", tool="claude", prompt="p", session_id="sid-1", resume=True
    )
    assert "--resume" in cmd and "--session-id" not in cmd
    assert cmd[cmd.index("--resume") + 1] == "sid-1"


def test_exec_command_adds_model_flag_when_given() -> None:
    cmd = _exec_cmd(name="c", tool="claude", prompt="p", session_id="s", model="opus")
    assert cmd[cmd.index("--model") + 1] == "opus"


def test_exec_command_passes_model_on_resume_too() -> None:
    cmd = _exec_cmd(
        name="c", tool="claude", prompt="p", session_id="s", resume=True, model="opus"
    )
    assert "--resume" in cmd
    assert cmd[cmd.index("--model") + 1] == "opus"


def test_exec_command_omits_model_flag_when_none() -> None:
    cmd = _exec_cmd(name="c", tool="claude", prompt="p", session_id="s")
    assert "--model" not in cmd


def test_exec_command_adds_effort_flag_when_given() -> None:
    cmd = _exec_cmd(name="c", tool="claude", prompt="p", session_id="s", effort="xhigh")
    assert cmd[cmd.index("--effort") + 1] == "xhigh"


def test_exec_command_omits_effort_flag_when_none() -> None:
    cmd = _exec_cmd(name="c", tool="claude", prompt="p", session_id="s")
    assert "--effort" not in cmd


def test_exec_command_rejects_unknown_tool() -> None:
    with pytest.raises(ValueError):
        _exec_cmd(name="c", tool="opencode", prompt="p", session_id="s")


# ── codex (#94) ────────────────────────────────────────────────────────


def test_exec_command_codex_first_turn() -> None:
    cmd = _exec_cmd(name="c", tool="codex", prompt="do it", session_id="unused-uuid")
    assert cmd[:5] == ["docker", "exec", "-w", "/workspace", "c"]
    # `codex exec` (no `resume`); the supplied session_id is NOT in the argv —
    # codex mints the thread_id itself on turn 1.
    assert cmd[cmd.index("codex") + 1] == "exec"
    assert "resume" not in cmd
    assert "unused-uuid" not in cmd
    assert "--json" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert cmd[-1] == "do it"


def test_exec_command_codex_resume_passes_thread_id() -> None:
    cmd = _exec_cmd(
        name="c", tool="codex", prompt="p", session_id="thread-7", resume=True
    )
    # `codex exec resume <thread_id>` — handle is positional, right after resume.
    idx = cmd.index("resume")
    assert cmd[idx - 1] == "exec"
    assert cmd[idx + 1] == "thread-7"
    assert "--json" in cmd
    assert cmd[-1] == "p"


def test_exec_command_codex_model_flag_and_effort_config_override() -> None:
    cmd = _exec_cmd(
        name="c", tool="codex", prompt="p", session_id="s", model="o3", effort="high"
    )
    assert cmd[cmd.index("-m") + 1] == "o3"
    # codex has no --effort flag; the level rides on its config override.
    assert "--effort" not in cmd
    assert cmd[cmd.index("-c") + 1] == "model_reasoning_effort=high"


def test_run_command_codex_env_mount_and_auth() -> None:
    cmd = _run_cmd(
        config_dir=Path("/work/run/agents/coder/claude_config"),
        config_mount="/codex_home",
        config_env_var="CODEX_HOME",
        auth_source_dir=Path("/home/u/.codex"),
        auth_files=["auth.json"],
        skills_dir=None,
    )
    assert "/work/run/agents/coder/claude_config:/codex_home" in cmd
    assert "/home/u/.codex/auth.json:/codex_home/auth.json" in cmd
    assert "CODEX_HOME=/codex_home" in cmd
    assert "CLAUDE_CONFIG_DIR=/claude_config" not in cmd
    # codex has no skill concept even if a dir were passed
    assert not any(a.endswith("/skills:ro") for a in cmd)


def test_container_name() -> None:
    assert (
        containers.container_name("ab12cd34", "coder") == "loom-develop-ab12cd34-coder"
    )


# resolve_auth_files was deleted in ARCH-2.E3 — its candidate-filtering contract
# now lives on Engine.auth_files, covered by tests/test_story_develop_engines.py.


# --- #403: re-sync the host's auth file into the bind-mounted inode ----------

_FRESH = b'{"claudeAiOauth": {"accessToken": "fresh"}}'
_STALE = b'{"claudeAiOauth": {"accessToken": "stale"}}'


def _exec_fake(calls: list[dict], *, container_inode: int | None, write_rc: int = 0):
    """A docker stand-in: `exec … stat -c %i <path>` answers with the inode
    the container's mount is pinned to (rc 1 when None: no such file / dead
    container); the `exec -i … sh -c 'cat > …'` write answers *write_rc*."""

    def fake_run(argv, **kw):
        calls.append({"argv": list(argv), **kw})
        if argv[:3] == ["docker", "exec", "-i"]:
            return subprocess.CompletedProcess(argv, write_rc, b"", b"boom")
        if container_inode is None:
            return subprocess.CompletedProcess(argv, 1, b"", b"No such file")
        return subprocess.CompletedProcess(
            argv, 0, f"{container_inode}\n".encode(), b""
        )

    return fake_run


def _resync(tmp_path: Path, files=(".credentials.json",)) -> list[str]:
    return containers.resync_auth_files(
        "loom-develop-abc-coder",
        config_mount="/claude_config",
        auth_source_dir=tmp_path,
        auth_files=list(files),
    )


def _host_inode(tmp_path: Path) -> int:
    return (tmp_path / ".credentials.json").stat().st_ino


def test_resync_auth_files_writes_the_host_file_into_a_stale_mounted_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single-file bind mount is pinned to the inode at container start; the
    CLI refreshes the host file by rename, so a running container keeps the
    stale inode. The re-sync sees the container's inode differs from the
    host file's and streams the host's CURRENT bytes through
    `docker exec -i … cat > <mount>/<file>` — an in-place write reaches the
    mounted inode where a host-side copy never would."""
    (tmp_path / ".credentials.json").write_bytes(_FRESH)
    calls: list[dict] = []
    stale = _host_inode(tmp_path) + 1
    monkeypatch.setattr(
        containers.subprocess, "run", _exec_fake(calls, container_inode=stale)
    )
    synced = _resync(tmp_path, (".credentials.json", "missing.json"))
    assert synced == [".credentials.json"]
    probe, write = calls  # the absent candidate is skipped before any exec
    assert probe["argv"] == [
        "docker",
        "exec",
        "loom-develop-abc-coder",
        "stat",
        "-c",
        "%i",
        "/claude_config/.credentials.json",
    ]
    assert write["argv"] == [
        "docker",
        "exec",
        "-i",
        "loom-develop-abc-coder",
        "sh",
        "-c",
        "cat > /claude_config/.credentials.json",
    ]
    assert write["input"] == _FRESH
    assert probe["timeout"] and write["timeout"]  # never hangs on a dead daemon


def test_resync_auth_files_never_writes_through_an_aliased_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #405 review (High): when no host rename has happened the mount
    still IS the host's live credentials file. A byte comparison cannot
    prove otherwise — an in-place refresh by another process between the
    two reads makes the bytes differ while the inode is shared, and the
    write would roll the operator's live login back to the older snapshot.
    Same inode → never written, whatever the bytes say."""
    (tmp_path / ".credentials.json").write_bytes(_STALE)  # our snapshot: older
    calls: list[dict] = []
    monkeypatch.setattr(
        containers.subprocess,
        "run",
        _exec_fake(calls, container_inode=_host_inode(tmp_path)),
    )
    assert _resync(tmp_path) == []
    heads = [c["argv"][:3] for c in calls]
    assert heads == [["docker", "exec", "loom-develop-abc-coder"]]  # the probe only


def test_resync_auth_files_snapshots_inode_and_bytes_from_one_open_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inode compared is the inode of the very file whose bytes are
    written (one fd, fstat + read), so a host rename between the two cannot
    pair a new inode with old bytes."""
    src = tmp_path / ".credentials.json"
    src.write_bytes(_FRESH)
    seen: list[tuple[int, bytes]] = []
    real_open = open

    def spy_open(path, mode="r", *a, **k):
        fh = real_open(path, mode, *a, **k)
        if mode == "rb":
            import os as _os

            seen.append((_os.fstat(fh.fileno()).st_ino, Path(path).read_bytes()))
        return fh

    monkeypatch.setattr("builtins.open", spy_open)
    calls: list[dict] = []
    monkeypatch.setattr(
        containers.subprocess, "run", _exec_fake(calls, container_inode=-1)
    )
    assert _resync(tmp_path) == [".credentials.json"]
    assert seen == [(src.stat().st_ino, _FRESH)]


def test_resync_auth_files_skips_a_file_the_container_never_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate absent at container start was never bind-mounted: writing
    it would create a plaintext copy inside the per-run config dir on the
    host. `stat` failing in-container (also: a dead container) → skipped."""
    (tmp_path / ".credentials.json").write_bytes(_FRESH)
    calls: list[dict] = []
    monkeypatch.setattr(
        containers.subprocess, "run", _exec_fake(calls, container_inode=None)
    )
    assert _resync(tmp_path) == []
    assert len(calls) == 1 and calls[0]["argv"][:3] != ["docker", "exec", "-i"]


def test_resync_auth_files_never_installs_an_empty_or_non_json_host_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI mid-rewrite can leave the host file empty; `cat >` with empty
    stdin would truncate the mounted credentials to nothing."""
    calls: list[dict] = []
    monkeypatch.setattr(
        containers.subprocess, "run", _exec_fake(calls, container_inode=-1)
    )
    for bad in (b"", b"not json"):
        (tmp_path / ".credentials.json").write_bytes(bad)
        assert _resync(tmp_path) == []
    assert calls == []  # no exec at all


def test_resync_auth_files_reports_only_what_landed_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed write is not 'landed'; a hung docker (TimeoutExpired) or a
    missing binary (OSError) must not escape into the reaction loop — an
    unhandled exception there would turn `infra_failed` into `internal` and
    lose the auth diagnosis (opus review, High)."""
    (tmp_path / ".credentials.json").write_bytes(_FRESH)
    calls: list[dict] = []
    monkeypatch.setattr(
        containers.subprocess,
        "run",
        _exec_fake(calls, container_inode=-1, write_rc=1),
    )
    assert _resync(tmp_path) == []

    def hung(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr(containers.subprocess, "run", hung)
    assert _resync(tmp_path) == []

    def no_docker(argv, **kw):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(containers.subprocess, "run", no_docker)
    assert _resync(tmp_path) == []
