"""Unit tests for the doctor check framework + vault probes (US15) + project
probes (Slice 4 US32)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.config import (
    LoomConfig,
    ObsidianSyncConfig,
    OrchestratorConfig,
    ProjectConfig,
)
from lithos_loom.doctor import (
    PROBE_FILENAME,
    CheckResult,
    _check_lithos_subdir_creatable,
    _check_probe_write_read,
    _check_vault_path_exists,
    format_results,
    run_project_checks,
    run_task_graph_checks,
    run_vault_checks,
)
from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import Note
from tests.support import FakeLithosClient, make_note


def _cfg(
    tmp_path: Path,
    *,
    with_obsidian_sync: bool = True,
) -> LoomConfig:
    """Minimal config with vault_path = tmp_path; obsidian_sync optional."""
    obs = None
    if with_obsidian_sync:
        obs = ObsidianSyncConfig(
            vault_path=tmp_path,
            tasks_file=Path("_lithos/tasks.md"),
        )
    return LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="lithos-orchestrator-test",
            lithos_url="http://localhost:8765",
        ),
        routes=(),
        obsidian_sync=obs,
    )


# ── _check_vault_path_exists ───────────────────────────────────────────


def test_vault_path_exists_passes_for_directory(tmp_path: Path) -> None:
    result = _check_vault_path_exists(tmp_path)
    assert result.passed is True
    assert result.name == "vault_path_exists"
    assert str(tmp_path) in result.message


def test_vault_path_exists_fails_when_missing(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-vault"
    result = _check_vault_path_exists(missing)
    assert result.passed is False
    assert "does not exist" in result.message
    assert str(missing) in result.message


def test_vault_path_exists_fails_when_path_is_a_file(tmp_path: Path) -> None:
    """A regular file at vault_path is a config error — operator pointed
    at the wrong thing. Report clearly."""
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("oops")
    result = _check_vault_path_exists(not_a_dir)
    assert result.passed is False
    assert "not a directory" in result.message


# ── _check_lithos_subdir_creatable ─────────────────────────────────────


def test_lithos_subdir_creatable_creates_when_missing(tmp_path: Path) -> None:
    assert not (tmp_path / "_lithos").exists()
    result = _check_lithos_subdir_creatable(tmp_path)
    assert result.passed is True
    assert (tmp_path / "_lithos").is_dir()


def test_lithos_subdir_creatable_idempotent_when_present(tmp_path: Path) -> None:
    """Pre-existing _lithos/ → passes without error (mkdir exist_ok=True)."""
    (tmp_path / "_lithos").mkdir()
    result = _check_lithos_subdir_creatable(tmp_path)
    assert result.passed is True


def test_lithos_subdir_creatable_fails_on_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permission flip / read-only mount → OSError → reported failure
    with the underlying message so the operator can act."""

    def _explode(self: Path, *, parents: bool = False, exist_ok: bool = False) -> None:
        raise PermissionError("simulated read-only filesystem")

    monkeypatch.setattr(Path, "mkdir", _explode)
    result = _check_lithos_subdir_creatable(tmp_path)
    assert result.passed is False
    assert "simulated read-only filesystem" in result.message


# ── _check_probe_write_read ────────────────────────────────────────────


def test_probe_write_read_roundtrip_succeeds_and_cleans_up(
    tmp_path: Path,
) -> None:
    (tmp_path / "_lithos").mkdir()
    result = _check_probe_write_read(tmp_path)
    assert result.passed is True
    assert "round-tripped" in result.message
    # Cleanup ran — no probe file lingering.
    assert not (tmp_path / "_lithos" / PROBE_FILENAME).exists()


def test_probe_write_read_fails_on_write_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "_lithos").mkdir()

    def _fail_write(self: Path, data: str, encoding: str | None = None) -> int:
        raise PermissionError("simulated write failure")

    monkeypatch.setattr(Path, "write_text", _fail_write)
    result = _check_probe_write_read(tmp_path)
    assert result.passed is False
    assert "simulated write failure" in result.message
    # The write didn't happen — no probe file present.
    assert not (tmp_path / "_lithos" / PROBE_FILENAME).exists()


def test_probe_write_read_fails_on_readback_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If readback returns content that doesn't match what was written
    (corrupted FS, weird mount, hardware fault), report mismatch and
    leave the probe on disk for inspection."""
    (tmp_path / "_lithos").mkdir()

    def _tamper(self: Path, encoding: str | None = None) -> str:
        return "not what was written\n"

    monkeypatch.setattr(Path, "read_text", _tamper)
    result = _check_probe_write_read(tmp_path)
    assert result.passed is False
    assert "readback mismatch" in result.message
    # Probe file remains on disk (we wrote it before reading) — useful
    # for the operator to inspect.
    assert (tmp_path / "_lithos" / PROBE_FILENAME).exists()


# ── run_vault_checks orchestration ─────────────────────────────────────


def test_run_vault_checks_returns_empty_when_no_obsidian_sync(
    tmp_path: Path,
) -> None:
    """Hosts without [obsidian_sync] configured shouldn't see vault
    failures — caller (CLI) reports the skip."""
    cfg = _cfg(tmp_path, with_obsidian_sync=False)
    assert run_vault_checks(cfg) == []


def test_run_vault_checks_short_circuits_on_first_failure(tmp_path: Path) -> None:
    """If vault_path doesn't exist, don't bother trying to mkdir or
    probe inside it — subsequent failures would cascade pointlessly."""
    cfg = _cfg(tmp_path / "absent")
    results = run_vault_checks(cfg)
    assert len(results) == 1
    assert results[0].name == "vault_path_exists"
    assert results[0].passed is False


def test_run_vault_checks_returns_three_passes_for_healthy_vault(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    results = run_vault_checks(cfg)
    assert [r.name for r in results] == [
        "vault_path_exists",
        "lithos_subdir_creatable",
        "probe_write_read_roundtrip",
    ]
    assert all(r.passed for r in results)


# ── format_results ─────────────────────────────────────────────────────


def test_format_results_renders_check_marks_and_messages() -> None:
    rendered = format_results(
        [
            CheckResult("alpha", True, "all good"),
            CheckResult("beta", False, "oh no"),
        ]
    )
    assert rendered == [
        "  ✓ alpha: all good",
        "  ✗ beta: oh no",
    ]


# ── run_project_checks (Slice 4 US32) ─────────────────────────────────


def _note(slug: str) -> Note:
    """Minimal project-context Note fixture — only ``slug`` is meaningful
    for the project-check join. Shaped so ``note_list``'s
    ``path_prefix='projects/'`` + ``tags=['project-context']`` filter keeps
    it and projects a ``NoteSummary`` whose ``slug`` joins to the TOML entry."""
    return make_note(
        f"id-{slug}",
        title=slug,
        tags=("project-context",),
        path=f"projects/{slug}/context.md",
        slug=slug,
    )


def _cfg_with_projects(tmp_path: Path, *slugs: str) -> LoomConfig:
    return LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="lithos-orchestrator-test",
            lithos_url="http://localhost:8765",
        ),
        projects={
            slug: ProjectConfig(name=slug, repo=tmp_path / slug) for slug in slugs
        },
    )


async def test_run_project_checks_empty_projects_returns_empty_list(
    tmp_path: Path,
) -> None:
    """No ``[projects]`` table → no Lithos round-trip, no results."""
    cfg = _cfg_with_projects(tmp_path)
    client = FakeLithosClient()
    results = await run_project_checks(cfg, client)
    assert results == []
    assert client.calls == []


async def test_run_project_checks_passes_when_slug_exists_in_lithos(
    tmp_path: Path,
) -> None:
    cfg = _cfg_with_projects(tmp_path, "lithos-loom")
    client = FakeLithosClient(notes=(_note("lithos-loom"),))
    results = await run_project_checks(cfg, client)
    assert len(results) == 1
    assert results[0].passed
    assert results[0].name == "toml_project[lithos-loom]"


async def test_run_project_checks_fails_when_slug_missing_from_lithos(
    tmp_path: Path,
) -> None:
    """A TOML stanza referencing a slug Lithos doesn't know about is
    a misconfiguration the operator should fix (either create the
    Lithos doc or remove the TOML entry)."""
    cfg = _cfg_with_projects(tmp_path, "ghost-project")
    client = FakeLithosClient(notes=(_note("real-project"),))
    results = await run_project_checks(cfg, client)
    assert len(results) == 1
    assert not results[0].passed
    assert "ghost-project" in results[0].message
    assert "either create one in Lithos or remove the TOML stanza" in results[0].message


async def test_run_project_checks_filters_query_to_project_context_only(
    tmp_path: Path,
) -> None:
    """The Lithos query MUST be ``path_prefix='projects/'`` AND
    ``tags=['project-context']`` so we don't accidentally credit a
    PRD or ADR doc as a project-context match."""
    cfg = _cfg_with_projects(tmp_path, "alpha")
    client = FakeLithosClient(notes=())
    await run_project_checks(cfg, client)
    [call] = client.calls_to("note_list")
    assert call["path_prefix"] == "projects/"
    assert call["tags"] == ["project-context"]


async def test_run_project_checks_orders_results_alphabetically(
    tmp_path: Path,
) -> None:
    cfg = _cfg_with_projects(tmp_path, "zeta", "alpha", "middle")
    client = FakeLithosClient(
        notes=(
            _note("alpha"),
            _note("middle"),
            _note("zeta"),
        )
    )
    results = await run_project_checks(cfg, client)
    names = [r.name for r in results]
    assert names == [
        "toml_project[alpha]",
        "toml_project[middle]",
        "toml_project[zeta]",
    ]


async def test_run_project_checks_does_not_fail_on_extra_lithos_slugs(
    tmp_path: Path,
) -> None:
    """Lithos-side projects WITHOUT a TOML entry are legitimate
    (other hosts may have automation; non-coding projects may never
    need one). The doctor doesn't surface them — that's
    ``project list``'s job."""
    cfg = _cfg_with_projects(tmp_path, "alpha")
    client = FakeLithosClient(notes=(_note("alpha"), _note("non-toml-project")))
    results = await run_project_checks(cfg, client)
    # Only the TOML entry is checked; "non-toml-project" is silent.
    assert len(results) == 1
    assert results[0].name == "toml_project[alpha]"
    assert results[0].passed


async def test_run_project_checks_unreachable_lithos_returns_single_failure(
    tmp_path: Path,
) -> None:
    """A transport failure (Lithos down, network drop) returns a
    single ``lithos_unreachable`` check rather than crashing the
    doctor run. Operators on flaky networks shouldn't be told their
    config is broken because Lithos was momentarily unreachable."""
    cfg = _cfg_with_projects(tmp_path, "alpha")
    client = FakeLithosClient()
    client.raise_on["note_list"] = OSError("connection refused")
    results = await run_project_checks(cfg, client)
    assert len(results) == 1
    assert results[0].name == "lithos_unreachable"
    assert not results[0].passed
    assert "connection refused" in results[0].message


async def test_run_project_checks_lithos_client_error_surfaces_as_unreachable(
    tmp_path: Path,
) -> None:
    """``LithosClientError`` (envelope-level failure from the MCP
    surface) also surfaces as ``lithos_unreachable`` so the
    operator-visible diagnostic is the same."""
    cfg = _cfg_with_projects(tmp_path, "alpha")
    client = FakeLithosClient()
    client.raise_on["note_list"] = LithosClientError("transport_failure", "down")
    results = await run_project_checks(cfg, client)
    assert len(results) == 1
    assert results[0].name == "lithos_unreachable"


# ── run_task_graph_checks (Epic G US1) ─────────────────────────────────


class _ReleasesCancelledBlocker(FakeLithosClient):
    """Non-conformant fake: (wrongly) treats a *cancelled* predecessor as
    satisfied, so a dependent becomes ready after its blocker is cancelled.
    Used to prove the probe's cancelled-blocker precondition guard bites."""

    def _blockers_for(self, task_id: str):  # type: ignore[override]
        return [b for b in super()._blockers_for(task_id) if b.status != "cancelled"]


async def test_task_graph_probe_passes_against_a_conformant_server() -> None:
    client = FakeLithosClient(agent_id="doctor-agent")
    results = await run_task_graph_checks(client, agent="doctor-agent")
    assert [r.name for r in results] == ["task_graph_extension"]
    assert results[0].passed, results[0].message
    # The probe cleans up after itself — nothing left open.
    assert await client.task_ready() == []


async def test_task_graph_probe_fails_when_extension_absent() -> None:
    """A server without the extension errors on the graph tools; the probe
    folds that into one failing check (the boot gate refuses on it)."""
    client = FakeLithosClient(agent_id="doctor-agent")
    client.raise_on["task_ready"] = LithosClientError("unknown_tool", "no such tool")
    results = await run_task_graph_checks(client, agent="doctor-agent")
    assert results[0].name == "task_graph_extension"
    assert not results[0].passed
    assert "unknown_tool" in results[0].message or "no such tool" in results[0].message


async def test_task_graph_probe_fails_when_cancelled_blocker_releases_dep() -> None:
    """The precondition guard: a server that wrongly releases a dependent whose
    blocker was cancelled must be caught (else ready-dispatch would run a task
    whose predecessor was cancelled)."""
    client = _ReleasesCancelledBlocker(agent_id="doctor-agent")
    results = await run_task_graph_checks(client, agent="doctor-agent")
    assert not results[0].passed
    assert "cancelled" in results[0].message.lower()


async def test_task_graph_probe_cleans_up_even_on_failure() -> None:
    """A mid-probe failure still cancels every task the probe created."""
    client = _ReleasesCancelledBlocker(agent_id="doctor-agent")
    await run_task_graph_checks(client, agent="doctor-agent")
    # Every probe task ended terminal (cancelled) — none linger as open work.
    assert await client.task_ready() == []
    assert await client.task_blocked() == []
